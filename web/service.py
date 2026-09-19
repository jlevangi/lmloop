"""What the dashboard does, separated from how it was asked.

`web/server.py` is HTTP: routing, auth, and turning a reply into bytes. The
operations themselves live here and know nothing about requests -- each takes
plain arguments and returns `(status, payload)`, so a caller decides how to
say it and a test can call one without a `Handler` or a socket.

The status code is part of the answer rather than a transport detail: "this run
already has a live loop" is a 409 in the same sense that it is a refusal, and
pushing that decision into the transport would mean the transport had to know
why. So the codes move with the operations, and `server.py` only forwards them.

These are not pure. They copy files, remove worktrees, launch processes and
open pull requests. That is what makes the split worth having: with the
transport out of the way, what remains in each function is exactly the part
that can lose something, in one place, testable directly.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import config as config_module
import runrecord
from web import runs as runs_module
from web import workspace
from preview import Preview


# Dashboard-launched loops are deliberately bounded even when a caller bypasses
# the browser.  A larger budget belongs in the CLI, not an HTTP request.
MAX_ITERATIONS = 1000
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_BYTES = 1_073_741_824


def _iteration_budget(value, default=None):
    raw = default if value is None else value
    if isinstance(raw, bool):
        return None
    try:
        budget = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return budget if 1 <= budget <= MAX_ITERATIONS else None


def create_project(payload: dict, config: dict) -> tuple[int, dict]:
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
    template = payload.get("template", "static-web")
    if not isinstance(template, str) or template != "static-web":
        return 400, {"error": "template must be `static-web`"}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        return 400, {
            "error": "name must be 1-64 characters of letters, digits, dot, dash or underscore"
        }
    if name in (".", "..") or name.startswith("."):
        return 400, {"error": "name may not start with a dot"}

    root = config["roots"][0]
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep):
        return 400, {"error": "name escapes the project root"}
    if target.exists():
        return 409, {"error": f"{name} already exists"}

    try:
        target.mkdir(parents=True)
        # A repository with no commits has no HEAD, and a worktree cannot be
        # branched off nothing -- so the first commit is part of creating the
        # project, not something the agent has to remember to do.
        readme = f"# {name}\n"
        if objective:
            readme += f"\n{objective}\n"
        (target / "README.md").write_text(readme)
        title = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        (target / "index.html").write_text(
            f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>{title}</title>"
            f"</head><body><main><h1>{title}</h1></main></body></html>\n"
        )
        (target / ".lmloop.toml").write_text(
            "[preview]\n"
            '# template = static-web\n'
            'command = ["python3", "-m", "http.server", "{port}"]\n'
            'path = "/"\nready_path = "/"\n'
        )
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm",
             f"Start {name}"],
        ):
            done = subprocess.run(argv, cwd=target, capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                return 500, {"error": (done.stderr or done.stdout).strip()[-400:]}
    except OSError as error:
        return 500, {"error": str(error)}

    return 200, {"id": name, "name": name, "path": str(target), "runs": 0}


def start_run(payload: dict, config: dict, lmloop_path: str) -> tuple[int, dict]:
    project_id = str(payload.get("project", ""))
    objective = str(payload.get("objective", "")).strip()
    if not objective:
        return 400, {"error": "an objective is required"}
    match = [p for p in runs_module.projects(config["roots"]) if p["id"] == project_id]
    if not match:
        return 400, {"error": "no such project"}

    iterations = _iteration_budget(
        payload.get("max_iterations"), config["default_max_iterations"])
    if iterations is None:
        return 400, {"error": f"max_iterations must be an integer from 1 to {MAX_ITERATIONS}"}

    argv = [config["python"], lmloop_path, "run", objective, "--detach"]
    for flag, key, default in (
        ("--model", "model", config["default_model"]),
        ("--thinking", "thinking", config["default_thinking"]),
    ):
        value = str(payload.get(key) or default).strip()
        if value:
            argv += [flag, value]
    argv += ["--max-iterations", str(iterations)]

    result = subprocess.run(
        argv, cwd=match[0]["path"], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return 500, {"error": (result.stderr or result.stdout).strip()[-800:]}
    return 200, {"started": result.stdout.strip()}


def archive_run(project: dict, run_dir: Path) -> tuple[int, dict]:
    """Copy the run's record out of its worktree, then drop the worktree."""

    if runs_module.is_archived(run_dir):
        return 400, {"error": "already archived"}
    holder = runs_module._holder(run_dir)
    if holder:
        return 409, {
            "error": f"this run has a live loop (pid {holder}); stop it first",
        }

    # Correct regardless of `[worktree] root`: `.lmloop/runs/<id>` is a
    # fixed relative layout under wherever the worktree actually is, so
    # this needs no `runrecord.resolved_worktree` fallback the way branch
    # resolution below does -- see runrecord.py's module docstring.
    worktree = run_dir.parents[2]
    target = runs_module.archive_target(project["id"], run_dir.name)
    if target.exists():
        return 409, {
            "error": f"archive already exists at {target}; worktree left alone",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    # Refuse any symlink in the record before copying it.  `shutil.copytree`
    # follows one by default, so a link pointing outside the run would have
    # dragged another tree's contents into the archive -- and the verification
    # below would have called it a match.  Walk without following: a symlink is
    # a claim about a path that is not this record's, and the archive is not
    # the place to honour it.
    symlinks = []
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            symlinks.append(path)
    if symlinks:
        return 500, {"error": f"refusing to archive a run containing symlinks: "
                               f"{symlinks[0].relative_to(run_dir)}"}
    staging = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=target.parent))
    try:
        shutil.copytree(run_dir, staging, dirs_exist_ok=True, symlinks=True)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        return 500, {"error": f"archive copy failed: {error}"}

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
        return 500, {"error": "archive verification failed; worktree left alone"}

    # Bound the archive before it is published.  The record is a few
    # directories and a log, so this is a tripwire rather than a limit anyone
    # expects to hit; it exists so a run that has been pointed at a tree of
    # a million files cannot spend minutes copying it.
    entries = sum(1 for _ in staging.rglob("*"))
    bytes_ = sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())
    if entries > MAX_ARCHIVE_ENTRIES or bytes_ > MAX_ARCHIVE_BYTES:
        shutil.rmtree(staging, ignore_errors=True)
        return 500, {"error": f"archive too large ({entries} entries, {bytes_} bytes); "
                               f"worktree left alone"}
    try:
        staging.rename(target)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        return 500, {"error": f"archive publish failed: {error}"}

    # `.lmloop` is ignored, so even a worktree with no user changes cannot be
    # removed normally while this verified source copy remains inside it.
    # Delete only the record now held byte-for-byte in the archive.  Any other
    # ignored runtime data makes the non-forced git removal refuse safely.
    settings = worktree / ".pi" / "settings.json"
    settings_bytes = settings.read_bytes() if settings.is_file() else None
    links = {}
    for name in config_module.load(
            Path(project["path"]), strict=False)["worktree"].get("link") or []:
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
        result = workspace.remove_worktree(project["path"], worktree)
    except subprocess.TimeoutExpired:
        restore_source()
        return 500, {
            "error": f"run archived to {target}, but worktree removal timed out; "
                     "the source record was restored",
        }
    if result.returncode != 0:
        restore_source()
        return 500, {
            "error": f"run archived to {target}, but the worktree still has "
                     f"other files and was not removed; the source record was restored: "
                     f"{(result.stderr or result.stdout).strip()[-300:]}",
        }
    return 200, runs_module.summarise(project, target)


def control(project: dict, run_dir: Path, action: str, payload: dict,
            config: dict, lmloop_path: str) -> tuple[int, dict] | None:
    """Pause, resume, stop, or continue -- all but one are a file touch.

    The loop polls for these itself, so nothing here needs to know whether the
    run is alive, own its pid, or still be running when it acts.

    Returns `None` for an action this does not own. Archive, delete and PR are
    dispatched by the caller: routing is the transport's job, and these five are
    the ones that are an operation rather than a destination.
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
        iterations = _iteration_budget(payload.get("iterations"), 3)
        if iterations is None:
            return 400, {"error": f"iterations must be an integer from 1 to {MAX_ITERATIONS}"}
        # A run that still has a live loop does not need continuing, and
        # starting a second one puts two loops in one worktree.  Refused here
        # rather than by the child, because the child's complaint goes to a pipe
        # nobody reads and the button just looks broken.
        holder = runs_module._holder(run_dir)
        if holder:
            return 409, {
                "error": f"this run already has a loop (pid {holder});"
                         " resume it instead of continuing it",
            }
        argv = [
            config["python"], lmloop_path, "resume", run_dir.name,
            "--iterations", str(iterations),
        ]
        for flag, key in (("--model", "model"), ("--thinking", "thinking")):
            if payload.get(key):
                argv += [flag, str(payload[key])]
        # Every sentinel, PAUSE included.  "Continue" is the button for a run
        # that has stopped, and a run is just as stopped when it is holding on
        # PAUSE -- leaving that one behind spawned a second loop that went
        # straight back into the hold, so the button did nothing and said
        # nothing about why.
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
                return 500, {
                    "error": log_path.read_text().strip()[-500:] or "resume failed",
                }
        except subprocess.TimeoutExpired:
            pass  # still running after a second and a half: it started
    else:
        return None
    return 200, runs_module.summarise(project, run_dir)


def _preview(run_dir: Path, fallback_project_path: Path | None = None) -> Preview:
    """Preview policy from the exact checkout the button will serve."""
    worktree = run_dir.parents[2] if len(run_dir.parents) >= 3 else (fallback_project_path or run_dir)
    return Preview(run_dir, config_module.load(worktree, strict=False))


def preview_status(project: dict, run_dir: Path) -> tuple[int, dict]:
    """Read preview state without waiting for startup or network readiness."""
    if runs_module.is_archived(run_dir):
        return 200, {"enabled": False, "state": "disabled", "url": "", "url_template": ""}
    return 200, _preview(run_dir, Path(project["path"])).status()


def preview_control(project: dict, run_dir: Path, action: str) -> tuple[int, dict]:
    """Start, stop, or restart only the preview belonging to this live run."""
    if runs_module.is_archived(run_dir):
        return 400, {"error": "archived runs cannot have previews"}
    if action not in {"start", "stop", "restart"}:
        return 400, {"error": f"unknown preview action {action}"}
    preview = _preview(run_dir, Path(project["path"]))
    return 200, getattr(preview, action)()


def delete_run(project: dict, run_dir: Path, payload: dict) -> tuple[int, dict]:
    """Permanently remove an archived run.  Refuses anything else."""
    if not runs_module.is_archived(run_dir):
        return 400, {
            "error": "archive this run before deleting it, so the removal "
                     "of its worktree and the loss of its record are two "
                     "separate decisions",
        }
    start = runrecord.latest_run_start(runs_module._events(run_dir))
    branch = runrecord.resolved_branch(run_dir, start)
    dropped = None
    if payload.get("branch"):
        # -D, not -d: the branch is usually unmerged, which is exactly the case
        # the caller is saying they do not want kept.
        result = workspace.delete_branch(project["path"], branch)
        dropped = branch if result.returncode == 0 else None
    try:
        shutil.rmtree(run_dir)
    except OSError as error:
        return 500, {"error": f"delete failed: {error}"}
    return 200, {"deleted": run_dir.name, "branch_deleted": dropped}


def open_pr(project: dict, run_dir: Path, payload: dict) -> tuple[int, dict]:
    """Push the run's branch and open a pull request for it."""
    start = runrecord.latest_run_start(runs_module._events(run_dir))
    branch = runrecord.resolved_branch(run_dir, start)
    repo = project["path"]
    base, ahead = workspace.pr_preflight(repo, branch)
    if base is None:
        return 404, {"error": f"no branch {branch}"}
    if ahead in ("", "0"):
        return 400, {"error": f"{branch} has no commits beyond {base}"}

    pushed = workspace.push_branch(repo, branch)
    if pushed.returncode != 0:
        return 500, {
            "error": f"push failed: {(pushed.stderr or pushed.stdout).strip()[-300:]}"
        }

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
    made = workspace.create_pull_request(repo, branch, base, title, body)
    if made.returncode != 0:
        message = (made.stderr or made.stdout).strip()
        # An existing PR is not a failure -- it is the answer to "where is the
        # PR for this run", so hand back the link rather than an error.
        existing = workspace.view_pull_request(repo, branch)
        if existing.returncode == 0 and existing.stdout.strip():
            return 200, {"url": existing.stdout.strip(), "existing": True}
        return 500, {"error": f"gh pr create failed: {message[-400:]}"}
    return 200, {"url": made.stdout.strip()}
