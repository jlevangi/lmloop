"""The lmloop dashboard: start, watch, pause and stop runs from a browser.

Two things make this much smaller than the predecessor dashboard it replaces.

**Runs are controlled by files, not by this process.**  Pausing is `touch PAUSE`
and stopping is `touch STOP`, which the loop notices on its own poll.  The predecessor dashboard
had to own a supervisor: track pids, reconcile them against processes it did not
start, signal process trees, and persist all of that so a restart did not orphan
a run.  Here the web app can crash, be restarted, or be replaced mid-run and
every control still works, because none of them were ever its to hold.

**The present is a file too.**  `status.json` is rewritten every couple of
seconds, so the dashboard reads state instead of replaying an append-only log to
its end.

The safety rule that follows from being able to start and stop agents: without
OIDC configured this server binds loopback only.  An unauthenticated dashboard
with a launch button does not belong on a network, and defaulting to "convenient"
is how that becomes someone's incident.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import config as config_module
from web import runs as runs_module
from web.auth import AVAILABLE as AUTH_AVAILABLE, OIDC

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LMLOOP = str(Path(__file__).resolve().parent.parent / "lmloop.py")

# Served pages may load only their own assets.  The dashboard has no third-party
# anything, so the strictest useful policy is also the one that costs nothing.
CSP = (
    "default-src 'self'; style-src 'self'; script-src 'self'; "
    "worker-src 'self'; img-src 'self' data:; connect-src 'self'"
)


def load_env(path: Path) -> None:
    """Read KEY=value lines without overriding the real environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def configure() -> dict:
    roots = [
        Path(item).expanduser()
        for item in os.environ.get("LMLOOP_WEB_ROOTS", str(Path.home() / "git")).split(":")
        if item.strip()
    ]
    return {
        "roots": roots,
        "host": os.environ.get("LMLOOP_WEB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("LMLOOP_WEB_PORT", "8082")),
        "poll_seconds": float(os.environ.get("LMLOOP_WEB_POLL_SECONDS", "3")),
        "hidden_poll_seconds": float(os.environ.get("LMLOOP_WEB_HIDDEN_POLL_SECONDS", "30")),
        "read_only": _flag("LMLOOP_WEB_READ_ONLY", False),
        "default_model": os.environ.get("LMLOOP_WEB_DEFAULT_MODEL", "llama-swap/local-fast"),
        "default_max_iterations": int(os.environ.get("LMLOOP_WEB_DEFAULT_MAX_ITERATIONS", "20")),
        "default_thinking": os.environ.get("LMLOOP_WEB_DEFAULT_THINKING", "low"),
        "python": os.environ.get("LMLOOP_WEB_PYTHON", "python3"),
    }


def build_auth() -> OIDC:
    return OIDC(
        os.environ.get("LMLOOP_WEB_OIDC_ISSUER", ""),
        os.environ.get("LMLOOP_WEB_OIDC_CLIENT_ID", ""),
        os.environ.get("LMLOOP_WEB_OIDC_CLIENT_SECRET", ""),
        os.environ.get("LMLOOP_WEB_PUBLIC_URL", ""),
        os.environ.get("LMLOOP_WEB_SESSION_SECRET", ""),
        int(os.environ.get("LMLOOP_WEB_SESSION_HOURS", "12")),
    )


# `pi --list-models` costs ~2.6s: it starts node, loads every extension, and
# asks llama-swap for its catalogue.  That is fine once and intolerable on every
# page load, so the answer is cached and the endpoint that needs it is separate
# from the one first paint waits on.  Models change when someone installs one.
_MODEL_CACHE: dict = {"at": 0.0, "value": None}
MODEL_CACHE_SECONDS = 300


def available_models(config: dict, force: bool = False) -> dict:
    """Model ids pi will accept, asked of pi rather than guessed.

    A dashboard that offers a model the agent cannot resolve produces a run that
    dies on its first request, minutes later, for a reason nobody can see from
    here.
    """
    fresh = time.monotonic() - _MODEL_CACHE["at"] < MODEL_CACHE_SECONDS
    if _MODEL_CACHE["value"] and fresh and not force:
        return _MODEL_CACHE["value"]
    try:
        result = subprocess.run(
            ["pi", "--list-models"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return {"models": [config["default_model"]], "model_source": "unavailable"}
    models = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("llama-swap", "9router"):
            models.append(f"{parts[0]}/{parts[1]}")
    result = {
        "models": models or [config["default_model"]],
        "model_source": "pi" if models else "fallback",
    }
    _MODEL_CACHE.update(at=time.monotonic(), value=result)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "lmloop-web"
    config: dict = {}
    auth: OIDC = None

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}")

    # -- plumbing ---------------------------------------------------------

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)

    def json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.send_header("Cache-Control", "no-store")
        BaseHTTPRequestHandler.end_headers(self)
        self.wfile.write(body)

    def static(self, name):
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self.json({"error": "not found"}, 404)
        body = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._headers(200, f"{kind}; charset=utf-8" if kind.startswith("text") else kind, len(body))
        self.send_header("Cache-Control", "no-cache")
        BaseHTTPRequestHandler.end_headers(self)
        self.wfile.write(body)

    def redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def cookies(self):
        from http.cookies import SimpleCookie

        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return {key: morsel.value for key, morsel in jar.items()}

    def session(self):
        if not self.auth.enabled:
            return {"name": "local", "csrf": "disabled"}
        return self.auth.session(self.cookies().get("lmloop_session"))

    def require_auth(self, api=False):
        session = self.session()
        if session:
            return session
        if api:
            self.json({"error": "authentication required"}, 401)
        else:
            self.redirect("/login")
        return None

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- run lookup -------------------------------------------------------

    def _resolve(self, project_id, run_id):
        for project in runs_module.projects(self.config["roots"]):
            if project["id"] != project_id:
                continue
            project_path = Path(project["path"])
            for run_dir in runs_module.run_dirs(project_path):
                if runs_module.route_id(project_path, run_dir) == run_id:
                    owning = runs_module.owner(project_path, run_dir)
                    return dict(project, path=str(owning)), run_dir
            # Archived runs have no worktree to walk.  Checked last, so a
            # re-used run id always resolves to the live run rather than the
            # archived copy of whatever held the name before it.
            target = runs_module.archive_target(project_id, run_id)
            if target.is_dir():
                return project, target
        return None, None

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            return self.json({"status": "ok", "oidc": self.auth.enabled, "read_only": self.config["read_only"]})
        if path == "/login":
            return self.static("login.html") if self.auth.enabled else self.redirect("/")
        if path == "/login/start":
            if not self.auth.enabled:
                return self.redirect("/")
            try:
                location, transaction = self.auth.begin()
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                return self.redirect("/login?" + urlencode({"error": str(error)}))
            return self.redirect(location, [
                f"lmloop_oidc={transaction}; Path=/oauth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"
            ])
        if path == "/oauth/callback":
            query = parse_qs(parsed.query)
            try:
                cookie = self.auth.callback(
                    query.get("code", [""])[0],
                    query.get("state", [""])[0],
                    self.cookies().get("lmloop_oidc", ""),
                )
            except Exception as error:  # noqa: BLE001
                return self.json({"error": f"OIDC callback failed: {error}"}, 400)
            # `Lax`, not `Strict`, and the difference is a whole extra click.
            # This response 302s to `/`, but that request is the tail of a
            # navigation chain that started at the identity provider, so the
            # browser still counts it as cross-site and withholds a `Strict`
            # cookie.  `/` then saw no session, bounced to `/login`, and the
            # sign-in button had to be pressed a second time to land anywhere.
            # `Lax` is sent on exactly this case -- a top-level GET navigation
            # -- and still withheld from cross-site POSTs and subresources;
            # state-changing calls are guarded by the CSRF token regardless.
            return self.redirect("/", [
                f"lmloop_session={cookie}; Path=/; Max-Age={self.auth.session_seconds}; HttpOnly; Secure; SameSite=Lax",
                "lmloop_oidc=; Path=/oauth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
            ])
        if path == "/logout":
            location = "/"
            if self.auth.enabled:
                location = self.auth.logout_url() + "?" + urlencode(
                    {"post_logout_redirect_uri": self.auth.public_url, "client_id": self.auth.client_id}
                )
            return self.redirect(location, [
                "lmloop_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            ])
        if path in ("/sw.js", "/manifest.json"):
            return self.static(path.lstrip("/"))

        if not path.startswith("/static/"):
            session = self.require_auth(path.startswith("/api/"))
            if not session:
                return

        if path == "/" or path == "/index.html":
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])

        if path == "/api/config":
            return self.json({
                "poll_seconds": self.config["poll_seconds"],
                "hidden_poll_seconds": self.config["hidden_poll_seconds"],
                "read_only": self.config["read_only"],
                "default_model": self.config["default_model"],
                "default_max_iterations": self.config["default_max_iterations"],
                "default_thinking": self.config["default_thinking"],
                "user": session["name"],
                "csrf": session["csrf"],
                "oidc": self.auth.enabled,
            })
        if path == "/api/models":
            # Fetched only when the new-run form opens, so first paint never
            # waits on a node process starting up.
            return self.json(available_models(self.config, force="refresh" in parsed.query))
        if path == "/api/projects":
            return self.json({"projects": runs_module.projects(self.config["roots"])})
        if path == "/api/runs":
            return self.json({"runs": runs_module.all_runs(self.config["roots"])})
        if path.startswith("/api/runs/"):
            parts = path[len("/api/runs/"):].split("/")
            if len(parts) == 2:
                project, run_dir = self._resolve(*parts)
                if not run_dir:
                    return self.json({"error": "no such run"}, 404)
                return self.json(runs_module.detail(project, run_dir))
        return self.json({"error": "not found"}, 404)

    # -- POST -------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path
        session = self.require_auth(api=True)
        if not session:
            return
        if self.config["read_only"]:
            return self.json({"error": "read-only mode"}, 403)
        if self.auth.enabled and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), session.get("csrf", "")
        ):
            return self.json({"error": "bad CSRF token"}, 403)

        if path == "/api/projects":
            return self.create_project(self.body())
        if path == "/api/runs":
            return self.start_run(self.body())
        if path.startswith("/api/runs/"):
            parts = path[len("/api/runs/"):].split("/")
            if len(parts) == 3:
                project, run_dir = self._resolve(parts[0], parts[1])
                if not run_dir:
                    return self.json({"error": "no such run"}, 404)
                return self.control(project, run_dir, parts[2], self.body())
        return self.json({"error": "not found"}, 404)

    def create_project(self, payload):
        """Make a new repository and hand it back ready to be run against.

        lmloop otherwise only works on code that already exists, which means an
        idea has to be turned into a git repository by hand before the loop can
        touch it.  This closes that gap: a name and an objective are enough to
        get from nothing to a working run.

        The name is the only untrusted path component in the system, so it is
        matched against a strict pattern rather than sanitised -- rejecting a
        bad name is always right, and guessing what someone meant by `../` is
        never right.
        """
        name = str(payload.get("name", "")).strip()
        objective = str(payload.get("objective", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            return self.json({
                "error": "name must be 1-64 characters of letters, digits, dot, dash or underscore"
            }, 400)
        if name in (".", "..") or name.startswith("."):
            return self.json({"error": "name may not start with a dot"}, 400)

        root = self.config["roots"][0]
        target = (root / name).resolve()
        if not str(target).startswith(str(root.resolve()) + os.sep):
            return self.json({"error": "name escapes the project root"}, 400)
        if target.exists():
            return self.json({"error": f"{name} already exists"}, 409)

        try:
            target.mkdir(parents=True)
            # A repository with no commits has no HEAD, and a worktree cannot be
            # branched off nothing -- so the first commit is part of creating the
            # project, not something the agent has to remember to do.
            readme = f"# {name}\n"
            if objective:
                readme += f"\n{objective}\n"
            (target / "README.md").write_text(readme)
            for argv in (
                ["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "-c", "commit.gpgsign=false", "commit", "-qm",
                 f"Start {name}"],
            ):
                done = subprocess.run(argv, cwd=target, capture_output=True, text=True, timeout=60)
                if done.returncode != 0:
                    return self.json({"error": (done.stderr or done.stdout).strip()[-400:]}, 500)
        except OSError as error:
            return self.json({"error": str(error)}, 500)

        return self.json({"id": name, "name": name, "path": str(target), "runs": 0})

    def start_run(self, payload):
        project_id = str(payload.get("project", ""))
        objective = str(payload.get("objective", "")).strip()
        if not objective:
            return self.json({"error": "an objective is required"}, 400)
        match = [p for p in runs_module.projects(self.config["roots"]) if p["id"] == project_id]
        if not match:
            return self.json({"error": "no such project"}, 400)

        argv = [self.config["python"], LMLOOP, "run", objective, "--detach"]
        for flag, key, default in (
            ("--model", "model", self.config["default_model"]),
            ("--thinking", "thinking", self.config["default_thinking"]),
        ):
            value = str(payload.get(key) or default).strip()
            if value:
                argv += [flag, value]
        iterations = payload.get("max_iterations") or self.config["default_max_iterations"]
        argv += ["--max-iterations", str(int(iterations))]

        result = subprocess.run(
            argv, cwd=match[0]["path"], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return self.json({"error": (result.stderr or result.stdout).strip()[-800:]}, 500)
        return self.json({"started": result.stdout.strip()})

    def control(self, project, run_dir, action, payload):
        """Pause, resume, stop, or continue -- all but one are a file touch.

        The loop polls for these itself, so nothing here needs to know whether
        the run is alive, own its pid, or still be running when it acts.
        """
        if action == "pause":
            (run_dir / "PAUSE").touch()
        elif action == "resume":
            (run_dir / "PAUSE").unlink(missing_ok=True)
        elif action == "stop":
            (run_dir / "STOP").touch()
        elif action == "stop-now":
            # The stop that does not wait for the iteration to finish.  Both
            # sentinels, so every reader that only knows about STOP still sees a
            # run that is stopping -- see rundir.stop_now_requested.
            (run_dir / "STOP-NOW").touch()
            (run_dir / "STOP").touch()
        elif action == "continue":
            # The one that needs a process: the run has already exited, and more
            # iterations mean starting the loop again on the same worktree.
            iterations = int(payload.get("iterations") or 3)
            # A run that still has a live loop does not need continuing, and
            # starting a second one puts two loops in one worktree.  Refused
            # here rather than by the child, because the child's complaint goes
            # to a pipe nobody reads and the button just looks broken.
            holder = runs_module._holder(run_dir)
            if holder:
                return self.json({
                    "error": f"this run already has a loop (pid {holder});"
                             " resume it instead of continuing it",
                }, 409)
            argv = [
                self.config["python"], LMLOOP, "resume", run_dir.name,
                "--iterations", str(iterations),
            ]
            for flag, key in (("--model", "model"), ("--thinking", "thinking")):
                if payload.get(key):
                    argv += [flag, str(payload[key])]
            # Every sentinel, PAUSE included.  "Continue" is the button for a
            # run that has stopped, and a run is just as stopped when it is
            # holding on PAUSE -- leaving that one behind spawned a second loop
            # that went straight back into the hold, so the button did nothing
            # and said nothing about why.
            (run_dir / "STOP").unlink(missing_ok=True)
            (run_dir / "STOP-NOW").unlink(missing_ok=True)
            (run_dir / "PAUSE").unlink(missing_ok=True)
            # To a file, not to DEVNULL: when this fails it fails in the first
            # second, and throwing the reason away is what made a dead button
            # indistinguishable from a working one.
            log_path = run_dir / "continue.log"
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    argv, cwd=project["path"], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
            try:
                if child.wait(timeout=1.5) != 0:
                    return self.json(
                        {"error": log_path.read_text().strip()[-500:] or "resume failed"},
                        500,
                    )
            except subprocess.TimeoutExpired:
                pass  # still running after a second and a half: it started
        elif action == "archive":
            return self.archive_run(project, run_dir)
        elif action == "delete":
            return self.delete_run(project, run_dir, payload)
        elif action == "pr":
            return self.open_pr(project, run_dir, payload)
        else:
            return self.json({"error": f"unknown action {action}"}, 400)
        return self.json(runs_module.summarise(project, run_dir))

    # -- archive, delete, PR ----------------------------------------------
    #
    # CLAUDE.md's first invariant is that nothing is ever discarded, and these
    # three are the only code in the project that removes anything.  They are
    # built so that no single action destroys evidence:
    #
    #   archive  copies the run out, *verifies the copy*, then asks git to remove
    #            only a clean worktree -- never with --force -- and leaves the
    #            branch alone.  The run stays readable in the dashboard.
    #   delete   refuses to run on anything that has not been archived first,
    #            so the destructive step is always the second one.

    def archive_run(self, project, run_dir):
        """Copy the run's record out of its worktree, then drop the worktree."""
        import hashlib
        import shutil
        import tempfile

        if runs_module.is_archived(run_dir):
            return self.json({"error": "already archived"}, 400)
        holder = runs_module._holder(run_dir)
        if holder:
            return self.json(
                {"error": f"this run has a live loop (pid {holder}); stop it first"}, 409
            )

        worktree = run_dir.parents[2]
        target = runs_module.archive_target(project["id"], run_dir.name)
        if target.exists():
            return self.json(
                {"error": f"archive already exists at {target}; worktree left alone"}, 409
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=target.parent))
        try:
            shutil.copytree(run_dir, staging, dirs_exist_ok=True)
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            return self.json({"error": f"archive copy failed: {error}"}, 500)

        def contents(root):
            return {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()
            }

        # Exact paths and content, not file counts.  A stale target with the same
        # number of files is not a copy, and this check guards the only deletion
        # below: the original run record inside the worktree.
        before, after = contents(run_dir), contents(staging)
        if after != before:
            shutil.rmtree(staging, ignore_errors=True)
            return self.json(
                {"error": "archive verification failed; worktree left alone"},
                500,
            )
        try:
            staging.rename(target)
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            return self.json({"error": f"archive publish failed: {error}"}, 500)

        # `.lmloop` is ignored, so even a worktree with no user changes cannot be
        # removed normally while this verified source copy remains inside it.
        # Delete only the record now held byte-for-byte in the archive.  Any other
        # ignored runtime data makes the non-forced git removal refuse safely.
        settings = worktree / ".pi" / "settings.json"
        settings_bytes = settings.read_bytes() if settings.is_file() else None
        links = {}
        for name in config_module.load(Path(project["path"]))["worktree"].get("link") or []:
            linked = worktree / name
            if linked.is_symlink():
                links[linked] = os.readlink(linked)
        shutil.rmtree(run_dir)

        # lmloop also owns its generated pi workspace pointer and only the
        # configured environment links.  Remove those links, never their targets.
        # Any regular file at one of these names is user data and is left alone.
        settings.unlink(missing_ok=True)
        for linked in links:
            linked.unlink()

        def restore_source():
            """Put lmloop-owned files back when Git retains the worktree."""
            if not run_dir.exists():
                shutil.copytree(target, run_dir)
            if settings_bytes is not None and not settings.exists():
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_bytes(settings_bytes)
            for linked, destination in links.items():
                if not linked.exists() and not linked.is_symlink():
                    linked.symlink_to(destination)

        try:
            result = subprocess.run(
                ["git", "worktree", "remove", str(worktree)],
                cwd=project["path"], capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            restore_source()
            return self.json(
                {"error": f"run archived to {target}, but worktree removal timed out; "
                          "the source record was restored"}, 500,
            )
        if result.returncode != 0:
            restore_source()
            return self.json(
                {"error": f"run archived to {target}, but the worktree still has "
                          f"other files and was not removed; the source record was restored: "
                          f"{(result.stderr or result.stdout).strip()[-300:]}"}, 500,
            )
        return self.json(runs_module.summarise(project, target))

    def delete_run(self, project, run_dir, payload):
        """Permanently remove an archived run.  Refuses anything else."""
        import shutil

        if not runs_module.is_archived(run_dir):
            return self.json(
                {"error": "archive this run before deleting it, so the removal "
                          "of its worktree and the loss of its record are two "
                          "separate decisions"}, 400,
            )
        branch = f"lmloop/{run_dir.name}"
        dropped = None
        if payload.get("branch"):
            # -D, not -d: the branch is usually unmerged, which is exactly the
            # case the caller is saying they do not want kept.
            result = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=project["path"], capture_output=True, text=True, timeout=30,
            )
            dropped = branch if result.returncode == 0 else None
        try:
            shutil.rmtree(run_dir)
        except OSError as error:
            return self.json({"error": f"delete failed: {error}"}, 500)
        return self.json({"deleted": run_dir.name, "branch_deleted": dropped})

    def open_pr(self, project, run_dir, payload):
        """Push the run's branch and open a pull request for it."""
        branch = f"lmloop/{run_dir.name}"
        repo = project["path"]

        def git(args, **kwargs):
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True,
                timeout=kwargs.pop("timeout", 120),
            )

        if git(["rev-parse", "--verify", branch]).returncode != 0:
            return self.json({"error": f"no branch {branch}"}, 404)
        base = (git(["symbolic-ref", "--short", "HEAD"]).stdout or "main").strip() or "main"
        ahead = git(["rev-list", "--count", f"{base}..{branch}"]).stdout.strip()
        if ahead in ("", "0"):
            return self.json({"error": f"{branch} has no commits beyond {base}"}, 400)

        pushed = git(["push", "-u", "origin", branch], timeout=180)
        if pushed.returncode != 0:
            return self.json(
                {"error": f"push failed: {(pushed.stderr or pushed.stdout).strip()[-300:]}"}, 500
            )

        objective = runs_module._read_text(run_dir / "prompt.md", 4000).strip()
        title = payload.get("title") or (
            objective.splitlines()[0][:100] if objective else branch
        )
        done, total = runs_module._plan_progress(runs_module._read_text(run_dir / "plan.md"))
        body = payload.get("body") or (
            objective
            + "\n\n---\n\n"
            + f"Plan: {done}/{total} steps. Branch `{branch}`, {ahead} commits.\n\n"
            + "Produced by lmloop. The run's plan, handoff and per-iteration "
            + f"record are in `.lmloop/runs/{run_dir.name}/`.\n"
        )
        made = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--base", base,
             "--title", title, "--body", body],
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
        if made.returncode != 0:
            message = (made.stderr or made.stdout).strip()
            # An existing PR is not a failure -- it is the answer to "where is
            # the PR for this run", so hand back the link rather than an error.
            existing = subprocess.run(
                ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
                cwd=repo, capture_output=True, text=True, timeout=60,
            )
            if existing.returncode == 0 and existing.stdout.strip():
                return self.json({"url": existing.stdout.strip(), "existing": True})
            return self.json({"error": f"gh pr create failed: {message[-400:]}"}, 500)
        return self.json({"url": made.stdout.strip()})


def serve(config: dict | None = None) -> int:
    config = config or configure()
    auth = build_auth()

    if not auth.enabled and config["host"] not in ("127.0.0.1", "localhost", "::1"):
        reason = "OIDC is not configured" if AUTH_AVAILABLE else (
            "PyJWT/requests are not installed for this interpreter, so OIDC cannot run"
        )
        raise SystemExit(
            f"lmloop web: refusing to bind {config['host']} because {reason}.\n"
            "  Configure LMLOOP_WEB_OIDC_*, or set LMLOOP_WEB_HOST=127.0.0.1 and reach it\n"
            "  over an SSH tunnel."
        )

    Handler.config = config
    Handler.auth = auth
    httpd = ThreadingHTTPServer((config["host"], config["port"]), Handler)
    scheme = "https" if auth.enabled else "http"
    print(f"lmloop web on {scheme}://{config['host']}:{config['port']}")
    print(f"  roots     {', '.join(str(root) for root in config['roots'])}")
    print(f"  auth      {'OIDC' if auth.enabled else 'none (loopback only)'}")
    print(f"  mode      {'read-only' if config['read_only'] else 'read-write'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
