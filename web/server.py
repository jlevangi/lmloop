"""The lmloop dashboard: start, watch, pause and stop runs from a browser.

Two things make this much smaller than the predecessor-dashboard dashboard it replaces.

**Runs are controlled by files, not by this process.**  Pausing is `touch PAUSE`
and stopping is `touch STOP`, which the loop notices on its own poll.  predecessor-dashboard
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
            for run_dir in runs_module.run_dirs(Path(project["path"])):
                if run_dir.name == run_id:
                    return project, run_dir
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
            return self.redirect("/", [
                f"lmloop_session={cookie}; Path=/; Max-Age={self.auth.session_seconds}; HttpOnly; Secure; SameSite=Strict",
                "lmloop_oidc=; Path=/oauth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
            ])
        if path == "/logout":
            location = "/"
            if self.auth.enabled:
                location = self.auth.logout_url() + "?" + urlencode(
                    {"post_logout_redirect_uri": self.auth.public_url, "client_id": self.auth.client_id}
                )
            return self.redirect(location, [
                "lmloop_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
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
        elif action == "continue":
            # The one that needs a process: the run has already exited, and more
            # iterations mean starting the loop again on the same worktree.
            iterations = int(payload.get("iterations") or 3)
            argv = [
                self.config["python"], LMLOOP, "resume", run_dir.name,
                "--iterations", str(iterations),
            ]
            for flag, key in (("--model", "model"), ("--thinking", "thinking")):
                if payload.get(key):
                    argv += [flag, str(payload[key])]
            (run_dir / "STOP").unlink(missing_ok=True)
            subprocess.Popen(
                argv, cwd=project["path"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            return self.json({"error": f"unknown action {action}"}, 400)
        return self.json(runs_module.summarise(project, run_dir))


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
