"""Every git invocation in lmloop lives here.

The invariant this module exists to protect: **no reset, no clean, no branch
deletion, no worktree removal.**  Grep this file for "reset" and you should find
only this docstring.  A slow model's half-finished work is the most expensive
thing in the system; nothing here may throw it away.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def repo_root(cwd: Path) -> Path:
    try:
        return Path(git(["rev-parse", "--show-toplevel"], cwd))
    except GitError as error:
        raise SystemExit(f"lmloop: {cwd} is not a git repository") from error


def head_commit(cwd: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd)


def current_branch(cwd: Path) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def is_clean(cwd: Path) -> bool:
    return not git(["status", "--porcelain"], cwd)


def add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path)], repo)


def exclude(repo: Path, patterns: list[str]) -> None:
    """Ignore run artifacts without touching the repo's tracked .gitignore.

    info/exclude is shared by the common git dir, so one write covers the repo
    and every worktree hanging off it.
    """
    exclude_path = Path(git(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], repo))
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text().splitlines() if exclude_path.exists() else []
    missing = [pattern for pattern in patterns if pattern not in existing]
    if not missing:
        return
    with exclude_path.open("a") as handle:
        if existing and existing[-1].strip():
            handle.write("\n")
        handle.write("# lmloop run artifacts\n")
        handle.write("".join(f"{pattern}\n" for pattern in missing))


def has_uncommitted(cwd: Path) -> bool:
    return bool(git(["status", "--porcelain"], cwd))


def commit_count(cwd: Path, base: str) -> int:
    out = git(["rev-list", "--count", f"{base}..HEAD"], cwd, check=False)
    return int(out) if out.isdigit() else 0


def diff_shortstat(cwd: Path, base: str) -> str:
    """Diff against the run's base commit, including uncommitted work.

    This is the only honest progress signal in the system.  The agent's own
    account of what it did is not evidence; free cloud routing combos once
    reported twelve successful iterations across 479 tool calls and zero writes.
    """
    return git(["diff", "--shortstat", base], cwd, check=False)


def log_oneline(cwd: Path, base: str, limit: int = 20) -> str:
    return git(["log", "--oneline", f"-{limit}", f"{base}..HEAD"], cwd, check=False)


def diff_stat(cwd: Path, base: str) -> str:
    return git(["diff", "--stat", base], cwd, check=False)


def commit_shortstat(cwd: Path, sha: str) -> str:
    return git(["show", "--shortstat", "--format=", sha], cwd, check=False).strip()


def commit_all(cwd: Path, message: str) -> str | None:
    """Stage and commit everything.  Returns the new sha, or None if no diff.

    gpg signing is disabled for the same reason the predecessor disables it: an unattended
    run cannot answer a passphrase prompt, and a run that blocks at 3am on one
    is indistinguishable from a run that hung.
    """
    git(["add", "-A"], cwd)
    if not git(["diff", "--cached", "--name-only"], cwd, check=False):
        return None
    git(
        ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "commit", "-m", message],
        cwd,
    )
    return head_commit(cwd)
