"""The run directory: everything a run leaves behind.

Layout, at ``<worktree>/.lmloop/runs/<run-id>/``::

    prompt.md                the objective, verbatim
    base-commit              the sha progress is measured against
    notes.md                 per-iteration log
    plan.md                  the objective decomposed; the agent maintains it
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

# A carried context summary buys its place in the next prompt by replacing file
# reads, so it can afford to be long -- but not unbounded.  12000 characters is
# roughly 3k tokens, 5% of a 57344-token window, against the ~40 reads that
# produced it.
_SUMMARY_LIMIT = 12000


def _cap(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """Bound anything carried into the next prompt.

    Preserving a handoff across a barren iteration re-adds a short preamble each
    time, so without a cap a run of empty iterations would grow the prompt
    without ever adding information to it.
    """
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n[truncated]"


class RunDir:
    def __init__(self, worktree: Path, run_id: str):
        self.run_id = run_id
        self.path = worktree / ".lmloop" / "runs" / run_id
        self.sessions = self.path / "sessions"
        self.log_path = self.path / "lmloop.log"
        self.notes_path = self.path / "notes.md"
        self.handoff_path = self.path / "handoff.md"
        self.plan_path = self.path / "plan.md"
        self.stop_path = self.path / "STOP"
        self.pause_path = self.path / "PAUSE"

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

    # -- plan -------------------------------------------------------------

    def read_plan(self) -> str:
        try:
            return self.plan_path.read_text().strip()
        except OSError:
            return ""

    def plan_mtime(self) -> float:
        try:
            return self.plan_path.stat().st_mtime
        except OSError:
            return 0.0

    def plan_problems(self) -> list[str]:
        """Corruption in the plan itself, which nothing else can see.

        `.lmloop/` is excluded from git, so the structural checks -- which work
        from what git says changed -- never look at the one file that steers the
        entire run.  Observed on one-project: an edit wrote a step twice, once in
        its original unchecked form and once checked, and because both the prompt
        and `_current_step` take the *first* unchecked line, the next iteration
        was sent to redo work that was already finished.

        A duplicated step is unambiguous damage rather than a decision, so it is
        reported like any other broken file and repair becomes the iteration's
        job.  The loop does not rewrite the plan itself: the plan is the agent's,
        and a harness that silently edits it is a harness whose state the agent
        can no longer trust.
        """
        seen: dict[str, int] = {}
        problems: list[str] = []
        for number, line in enumerate(self.read_plan().splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("- [", "* [")):
                continue
            # Compare the step text, ignoring whether it is checked: the same
            # step present both checked and unchecked is exactly the failure.
            text = " ".join(stripped[5:].split()).lower()
            if not text:
                continue
            if text in seen:
                problems.append(
                    f"plan.md:{number}: step duplicated from line {seen[text]} "
                    f"-- \"{text[:60]}\". The first unchecked copy is what the "
                    "next iteration is sent to do, so remove the stale one."
                )
            else:
                seen[text] = number
        return problems

    def plan_progress(self) -> tuple[int, int]:
        """(done, total) checkbox items in the plan.

        Counted rather than trusted from prose: the agent reports progress in
        its handoff, and a self-report is not evidence.  This is only for the
        log and the status line -- git remains what decides whether the run is
        getting anywhere.
        """
        done = total = 0
        for line in self.read_plan().splitlines():
            stripped = line.strip()
            if stripped.startswith(("- [", "* [")):
                total += 1
                if stripped[3:4].lower() == "x":
                    done += 1
        return done, total

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

    def last_compaction_summary(self, number: int) -> str:
        """The summary the agent wrote for itself the last time pi compacted.

        An iteration that overflows its context has already written a handoff --
        it just wrote it to pi's event stream instead of to disk.  pi compacts by
        asking the model to summarise its own state, and that summary is exactly
        what the next iteration needs: goal, constraints, what it read, what it
        decided, what to do next.  One harvested from one-project ran to 15 KB
        and included a self-correction about whether ``create_app()`` calls
        ``db.create_all()`` -- a fact that cost the agent forty file reads.

        Synthesising from ``git diff`` instead throws all of that away, and for a
        zero-diff iteration it says "(no changes)", which is nothing at all.

        The ``<read-files>`` trailer pi appends is dropped: in a fresh session it
        is a list of paths with no contents, and its only effect on the next
        iteration would be to invite the re-reading that caused the overflow.
        """
        path = self.iteration_jsonl(number)
        line = ""
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    if b'"compaction_end"' in raw:
                        line = raw.decode(errors="replace")
        except OSError:
            return ""
        try:
            summary = ((json.loads(line).get("result") or {}).get("summary") or "").strip()
        except ValueError:
            return ""

        summary = summary.split("<read-files>")[0].strip()
        if len(summary) > _SUMMARY_LIMIT:
            summary = summary[:_SUMMARY_LIMIT].rstrip() + "\n\n[summary truncated]"
        return summary

    def write_synthetic_handoff(self, number: int, diff_stat: str, carried: str = "") -> None:
        """Stand in for a handoff the agent never wrote.

        A missing handoff is a degraded iteration, never a discarded one: the
        work is already committed by the time this runs, and the next iteration
        still needs somewhere to start from.

        The one thing this must never do is *subtract*.  An iteration that
        achieves nothing -- no diff, no handoff, not even a context summary --
        used to overwrite the previous handoff with "(no changes)", which is how
        a 10 KB carried summary became nine lines of boilerplate and the next
        iteration went back to reading files from scratch.  Observed live on
        one-project between iterations 2 and 4.  When there is nothing new to
        say, the previous handoff is still the truth and is kept.
        """
        previous = self.read_handoff() if not carried else ""

        subject = f"iteration {number} ended without writing a handoff"
        if carried:
            subject += "; carrying its own context summary forward"
        elif previous and not diff_stat.strip():
            subject += "; the previous handoff still stands"
        lines = [
            subject,
            "",
            "The harness synthesised this from git.  What changed:",
            "",
            diff_stat or "(no changes)",
            "",
        ]
        if carried:
            lines += [
                "The agent wrote no handoff, but it overflowed its context at least",
                "once, and the summary it wrote for itself on the way out survives in",
                "the event stream.  It is reproduced verbatim below: this is what the",
                "last iteration had worked out before it ran out of room.",
                "",
                "Trust it.  Do NOT re-read the codebase to re-derive it -- that is what",
                "consumed the whole of the last iteration.  Start from its next steps",
                "and make one of your first tool calls a write.",
                "",
                "---",
                "",
                carried,
            ]
        elif previous and not diff_stat.strip():
            lines += [
                "It changed no files and left no context summary, so it has nothing to",
                "add.  The handoff below is the one that was already here, kept",
                "verbatim: it remains the most recent real account of where the work",
                "stands, and replacing it with this iteration's silence would cost the",
                "next iteration the orientation this one failed to earn.",
                "",
                "---",
                "",
                _cap(previous),
            ]
        else:
            lines += [
                "Next iteration: re-read the objective, check the diff above against it,",
                "and continue.  Write this file before you finish.",
            ]
        self.handoff_path.write_text("\n".join(lines) + "\n")

    # -- per-iteration paths ----------------------------------------------

    def iteration_jsonl(self, number: int) -> Path:
        return self.path / f"iteration-{number}.jsonl"

    def iteration_prompt(self, number: int) -> Path:
        return self.path / f"iteration-{number}-prompt.md"

    def gate_log(self, number: int) -> Path:
        return self.path / f"gate-{number}.log"

    # -- live status ------------------------------------------------------

    def write_status(self, state: dict) -> None:
        """Overwrite `status.json` with what the run is doing right now.

        The event log is an append-only history and answering "what is happening
        now" from it means parsing to the end of a file that reaches megabytes.
        This is one small file holding only the present, so a phone script, a
        status bar, or a web wrapper can read it without understanding lmloop.
        Written atomically because something is always reading it.
        """
        state = dict(state, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.path / "status.json.tmp"
        try:
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(self.path / "status.json")
        except OSError:
            pass  # a status file is never worth failing a run over

    # -- control ----------------------------------------------------------

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def paused(self) -> bool:
        return self.pause_path.exists()


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
