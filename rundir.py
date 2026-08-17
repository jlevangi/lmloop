"""The run directory: everything a run leaves behind.

Layout, at ``<worktree>/.lmloop/runs/<run-id>/``::

    prompt.md                the objective, verbatim
    base-commit              the sha progress is measured against
    notes.md                 per-iteration log
    handoff.md               rewritten each iteration by the agent
    lmloop.log               JSONL event stream
    iteration-<n>.jsonl      raw pi event stream
    iteration-<n>-prompt.md  exactly what was sent
    gate-<n>.log             gate command output
    sessions/iter-<n>.jsonl  pi session transcript
    STOP                     sentinel; stop after the current iteration

The event names in ``lmloop.log`` and the heading format in ``notes.md`` are
the predecessor's, deliberately.  They are a wire format the predecessor-dashboard dashboard already
parses, and copying a format costs nothing while inventing one costs a UI.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class RunDir:
    def __init__(self, worktree: Path, run_id: str):
        self.run_id = run_id
        self.path = worktree / ".lmloop" / "runs" / run_id
        self.sessions = self.path / "sessions"
        self.log_path = self.path / "lmloop.log"
        self.notes_path = self.path / "notes.md"
        self.handoff_path = self.path / "handoff.md"
        self.stop_path = self.path / "STOP"

    # -- creation ---------------------------------------------------------

    def create(self, prompt: str, base_commit: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.sessions.mkdir(parents=True, exist_ok=True)
        (self.path / "prompt.md").write_text(prompt.rstrip() + "\n")
        (self.path / "base-commit").write_text(base_commit + "\n")
        self.notes_path.write_text(
            f"# lmloop run: {self.run_id}\n\n"
            f"Objective: see .lmloop/runs/{self.run_id}/prompt.md\n\n"
            "## Iteration Log\n"
        )

    @property
    def base_commit(self) -> str:
        return (self.path / "base-commit").read_text().strip()

    # -- event log --------------------------------------------------------

    def event(self, name: str, **fields) -> None:
        """Append one JSONL event.

        ``pid`` is what lets a reader separate two runs that share a directory;
        predecessor-dashboard's outcome parser keys on the last ``run:start`` pid and ignores
        every event that does not match it.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "event": name,
            **fields,
        }
        with self.log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    # -- notes ------------------------------------------------------------

    def append_notes(self, number: int, summary: str, changes: list[str], learnings: list[str]) -> None:
        parts = [f"\n### Iteration {number}\n", f"\n**Summary:** {summary}\n"]
        if changes:
            parts.append("\n**Changes:**\n" + "".join(f"- {item}\n" for item in changes))
        if learnings:
            parts.append("\n**Learnings:**\n" + "".join(f"- {item}\n" for item in learnings))
        with self.notes_path.open("a") as handle:
            handle.write("".join(parts))

    # -- handoff ----------------------------------------------------------

    def read_handoff(self) -> str:
        try:
            return self.handoff_path.read_text().strip()
        except OSError:
            return ""

    def handoff_mtime(self) -> float:
        try:
            return self.handoff_path.stat().st_mtime
        except OSError:
            return 0.0

    def write_synthetic_handoff(self, number: int, diff_stat: str) -> None:
        """Stand in for a handoff the agent never wrote.

        A missing handoff is a degraded iteration, never a discarded one: the
        work is already committed by the time this runs, and the next iteration
        still needs somewhere to start from.
        """
        self.handoff_path.write_text(
            f"iteration {number} ended without writing a handoff\n"
            "\n"
            "The harness synthesised this from git.  What changed:\n"
            "\n"
            f"{diff_stat or '(no changes)'}\n"
            "\n"
            "Next iteration: re-read the objective, check the diff above against it,\n"
            "and continue.  Write this file before you finish.\n"
        )

    # -- per-iteration paths ----------------------------------------------

    def iteration_jsonl(self, number: int) -> Path:
        return self.path / f"iteration-{number}.jsonl"

    def iteration_prompt(self, number: int) -> Path:
        return self.path / f"iteration-{number}-prompt.md"

    def gate_log(self, number: int) -> Path:
        return self.path / f"gate-{number}.log"

    # -- control ----------------------------------------------------------

    def stop_requested(self) -> bool:
        return self.stop_path.exists()


def make_run_id(prompt: str) -> str:
    """``<date>-<slug>-<hash>``.

    the predecessor's hash suffix is a good collision strategy; leading with the date is
    the part it is missing, so a directory of runs sorts chronologically.
    """
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:24].rstrip("-")
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:6]
    return f"{time.strftime('%Y-%m-%d')}-{slug or 'run'}-{digest}"
