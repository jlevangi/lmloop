"""Cheap structural checks on whatever an iteration actually changed.

The project gate is whatever its owner thought to configure, which means a repo
gets checked on the dimensions someone anticipated.  The damage an editing agent
does is not project-specific: on one-project an agent under context pressure
wrote a fragment of CSS twice -- an orphaned declaration and an extra `}` that
closed a media query early -- and six iterations committed it as a success,
because that project's gate was `compileall -q backend` and a stylesheet is
invisible to it.

So these run on every iteration regardless of configuration, over the files git
says changed, and they look for the specific ways an edit goes wrong rather than
for whether the program is correct:

* a file that no longer parses at all,
* a conflict marker left in the tree,
* a block of lines pasted twice in a row.

They are deliberately not linters.  Style is the agent's business and a noisy
check is one that gets ignored; every rule here answers "did the edit land
intact", which is a question the agent cannot see the answer to and the operator
should not have to.

Nothing here blocks a commit.  The invariant is that work is never discarded, so
a failure is recorded, shown in the next iteration's prompt, and left for the
agent to fix -- which is precisely the loop working as intended.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

# Anything larger and it is a generated file, a lockfile or a vendored blob --
# none of which an agent edits by hand, and all of which are slow to scan.
MAX_BYTES = 400_000

CONFLICT = re.compile(r"^(<{7}|={7}|>{7})(\s|$)", re.M)

# A repeated run this long is not a coincidence: two identical adjacent lines
# happen constantly in real code (closing braces, blank lines), three or more do
# not, and that is the shape a doubled edit leaves behind.
DUPLICATE_RUN = 3


def _text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _braces(text: str, name: str) -> list[str]:
    """Balance check for brace languages, comments stripped first."""
    problems = []
    if text.count("/*") != text.count("*/"):
        problems.append(f"{name}: unclosed /* comment")
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    depth = 0
    for number, line in enumerate(stripped.splitlines(), 1):
        for char in line:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    problems.append(f"{name}:{number}: unmatched '}}'")
                    depth = 0
    if depth:
        problems.append(f"{name}: {depth} unclosed block(s)")
    return problems


def _duplicated_block(text: str, name: str) -> list[str]:
    """Find a run of lines immediately repeated.

    This is the signature of the failure that prompted the module: an edit tool
    applying the same hunk twice, which leaves the second copy sitting directly
    under the first.  Blank and trivially short lines are ignored, or every
    closing brace in the file would match.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    meaningful = [index for index, line in enumerate(lines) if len(line.strip()) > 3]
    for start in range(len(meaningful)):
        for length in range(DUPLICATE_RUN, min(12, (len(meaningful) - start) // 2 + 1)):
            first = [lines[i] for i in meaningful[start:start + length]]
            second = [lines[i] for i in meaningful[start + length:start + 2 * length]]
            if len(second) == length and first == second and any(l.strip() for l in first):
                return [f"{name}:{meaningful[start] + 1}: {length} lines appear twice in a row"]
    return []


def _node_available() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def changed_files(worktree: Path, base: str) -> list[str]:
    """Everything this run has touched, tracked or not.

    `git diff` alone misses untracked files, and a new file is the most likely
    thing an iteration produced -- a fresh test module, a new stylesheet
    partial.  Checking only modifications would skip exactly the work most
    worth checking.
    """
    names: list[str] = []
    for argv in (
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        result = subprocess.run(argv, cwd=str(worktree), capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.strip() and line not in names:
                names.append(line)
    return names


def run(worktree: Path, base: str) -> list[str]:
    """Every problem found in the files this run has touched."""
    problems: list[str] = []
    node = None

    for name in changed_files(worktree, base):
        path = worktree / name
        if not path.is_file():
            continue
        text = _text(path)
        if text is None:
            continue

        if CONFLICT.search(text):
            problems.append(f"{name}: conflict marker left in the file")

        suffix = path.suffix.lower()
        if suffix == ".py":
            try:
                compile(text, name, "exec")
            except SyntaxError as error:
                problems.append(f"{name}:{error.lineno}: {error.msg}")
        elif suffix == ".json":
            try:
                json.loads(text)
            except ValueError as error:
                problems.append(f"{name}: invalid JSON ({error})")
        elif suffix == ".toml":
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                problems.append(f"{name}: invalid TOML ({error})")
        elif suffix in (".css", ".scss"):
            problems += _braces(text, name)
        elif suffix in (".js", ".mjs", ".cjs"):
            if node is None:
                node = _node_available()
            if node:
                done = subprocess.run(
                    ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
                )
                if done.returncode != 0:
                    first = (done.stderr.strip().splitlines() or [""])[0]
                    problems.append(f"{name}: {first[:120]}")

        problems += _duplicated_block(text, name)

    return problems
