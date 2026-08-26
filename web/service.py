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

import shutil
import subprocess
from pathlib import Path

import runrecord
from web import runs as runs_module


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
        iterations = int(payload.get("iterations") or 3)
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
        result = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project["path"], capture_output=True, text=True, timeout=30,
        )
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

    def git(args, **kwargs):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            timeout=kwargs.pop("timeout", 120),
        )

    if git(["rev-parse", "--verify", branch]).returncode != 0:
        return 404, {"error": f"no branch {branch}"}
    base = (git(["symbolic-ref", "--short", "HEAD"]).stdout or "main").strip() or "main"
    ahead = git(["rev-list", "--count", f"{base}..{branch}"]).stdout.strip()
    if ahead in ("", "0"):
        return 400, {"error": f"{branch} has no commits beyond {base}"}

    pushed = git(["push", "-u", "origin", branch], timeout=180)
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
    made = subprocess.run(
        ["gh", "pr", "create", "--head", branch, "--base", base,
         "--title", title, "--body", body],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )
    if made.returncode != 0:
        message = (made.stderr or made.stdout).strip()
        # An existing PR is not a failure -- it is the answer to "where is the
        # PR for this run", so hand back the link rather than an error.
        existing = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=repo, capture_output=True, text=True, timeout=60,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return 200, {"url": existing.stdout.strip(), "existing": True}
        return 500, {"error": f"gh pr create failed: {message[-400:]}"}
    return 200, {"url": made.stdout.strip()}
