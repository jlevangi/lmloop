"""The only code in lmloop that removes a worktree, deletes a branch, or
reaches off the machine.

`gitops.py` is where every git invocation the *loop* makes lives, and it
contains no destructive one at all -- that is invariant 1, and a test greps its
string literals to keep it so. But the dashboard has always needed four things
that file will never hold: it removes a worktree when a run is archived, drops
a branch when an archived run is deleted, and pushes and opens a pull request
when somebody asks for one.

Those were spread through `web/server.py` as bare `subprocess.run` calls, which
meant the invariant was enforced in a file the destructive calls did not go
through -- and the test guarding it gave false comfort about the codebase as a
whole. They are gathered here instead: few, named, and greppable, so "what in
this project can destroy something" has an answer you can read in one screen.

**Nothing here decides whether it is allowed to act.** Every one of these runs
only after a guard in `web/service.py` has said so -- a verified byte-for-byte
copy before a worktree goes, an archived-only check before a branch does, a
live-loop refusal before either. Keeping the decision in the service and the
act here is the point: this file is the blast radius, not the policy.

Each returns the finished process rather than a verdict, because the callers
report git's own words back to the operator and a boolean would throw away the
only useful part of a failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def remove_worktree(repo: str | Path, worktree: str | Path,
                    timeout: int = 60) -> subprocess.CompletedProcess:
    """Remove a worktree, non-forced.

    Never `--force`. The refusal is load-bearing: git declines while the
    worktree still holds files nobody has accounted for, which is exactly the
    case where removing it would discard an agent's work. The caller restores
    the run's record and reports why -- see `service.archive_run`.
    """
    return subprocess.run(
        ["git", "worktree", "remove", str(worktree)],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout,
    )


def delete_branch(repo: str | Path, branch: str,
                  timeout: int = 30) -> subprocess.CompletedProcess:
    """Delete a branch.

    `-D`, not `-d`: the branch is usually unmerged, which is exactly the case
    the caller is saying they do not want kept. Only ever reached for a run
    whose record is already archived, so the commits are the only thing being
    dropped and the account of how they came about survives.
    """
    return subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout,
    )


def push_branch(repo: str | Path, branch: str,
                timeout: int = 180) -> subprocess.CompletedProcess:
    """Push a run's branch to `origin`.

    Not destructive, but outward: the first thing here that leaves the machine,
    and the first that another person can see.
    """
    return subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout,
    )


def create_pull_request(repo: str | Path, branch: str, base: str, title: str,
                        body: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Open a pull request through the GitHub CLI."""
    return subprocess.run(
        ["gh", "pr", "create", "--head", branch, "--base", base,
         "--title", title, "--body", body],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout,
    )


def view_pull_request(repo: str | Path, branch: str,
                      timeout: int = 60) -> subprocess.CompletedProcess:
    """The URL of an existing pull request for a branch, if there is one.

    Read-only, and here rather than in the service because it is the other half
    of `create_pull_request`: an existing PR is the answer to "where is the PR
    for this run" rather than a failure, and the two calls are only sensible
    read together.
    """
    return subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout,
    )
