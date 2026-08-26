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

import subprocess
from pathlib import Path

import runrecord
from web import runs as runs_module


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
