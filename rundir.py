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
    STOP-NOW                 sentinel; cut the current iteration short

The event names in ``lmloop.log`` and the heading format in ``notes.md`` are
inherited from the predecessor dashboard, deliberately.  They are a wire format
it already parses, and copying a format costs nothing while inventing one costs
a UI.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import runrecord

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
        self.status_path = self.path / "status.json"
        self.run_state_path = self.path / "run-state.json"
        self.pid_path = self.path / "loop.pid"
        self.stop_path = self.path / "STOP"
        self.stop_now_path = self.path / "STOP-NOW"
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
        the predecessor dashboard's outcome parser keys on the last ``run:start`` pid and ignores
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

    def read_events(self) -> list[dict]:
        """The run's own event log, parsed -- see `runrecord.read_events`."""
        return runrecord.read_events(self.path)

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
        entire run.  Observed on one project: an edit wrote a step twice, once in
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
        """(done, total) checkbox items in the plan -- see `runrecord.plan_progress`."""
        return runrecord.plan_progress(self.read_plan())

    def current_step(self) -> str:
        """The first unchecked plan step -- what the next iteration is here for.

        The *first* one, deliberately, and the same rule the prompt uses: a plan
        is ordered, so the earliest unfinished step is the one in play.  Anything
        that wants to talk about "the step that thrashed" has to agree with the
        prompt about which step that was, or it will name the wrong one.

        Only the backtick pair bracketing the whole step is trimmed -- see
        `runrecord.first_unchecked_step` for why this differs from the WebUI's
        own reading of the same plan.
        """
        return runrecord.first_unchecked_step(self.read_plan()).strip("`")

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

    def last_compaction_summary(self, number: int, agent_name: str = "pi") -> str:
        """The summary the agent wrote for itself the last time it compacted.

        An iteration that overflows its context has already written a handoff --
        it just wrote it to pi's event stream instead of to disk.  pi compacts by
        asking the model to summarise its own state, and that summary is exactly
        what the next iteration needs: goal, constraints, what it read, what it
        decided, what to do next.  One harvested from a real run ran to 15 KB
        and included a self-correction about whether ``create_app()`` calls
        ``db.create_all()`` -- a fact that cost the agent forty file reads.

        Synthesising from ``git diff`` instead throws all of that away, and for a
        zero-diff iteration it says "(no changes)", which is nothing at all.

        The ``<read-files>`` trailer pi appends is dropped: in a fresh session it
        is a list of paths with no contents, and its only effect on the next
        iteration would be to invite the re-reading that caused the overflow.
        """
        import harness

        try:
            agent = harness.get(agent_name)
        except SystemExit:
            return ""
        # The marker comes from the adapter because the event name does: omp
        # calls it `auto_compaction_end` where pi calls it `compaction_end`, and
        # scanning for one name found nothing in the other's stream while
        # looking exactly like an iteration that had never overflowed.  An agent
        # that does not compact declares no marker, never matches, and the loop
        # falls back to a git-synthesised handoff.
        if not agent.compaction_marker:
            return ""
        line = ""
        try:
            with self.open_iteration(number) as handle:
                for raw in handle:
                    if agent.compaction_marker in raw:
                        line = raw.decode(errors="replace")
        except (OSError, EOFError):
            return ""
        try:
            summary = agent.compaction_summary(json.loads(line))
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
        one repository between iterations 2 and 4.  When there is nothing new to
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
        """Where this iteration's raw stream is written.  Always uncompressed:
        `lmloop prune` archives it later, once nothing is writing to it."""
        return self.path / f"iteration-{number}.jsonl"

    def open_iteration(self, number: int):
        """Read an iteration's stream in binary, compressed or not.

        `lmloop prune` gzips finished streams, so anything that reads one has to
        accept either form -- otherwise pruning a run would silently break the
        compaction harvest, which is the single most valuable thing in the file.
        """
        plain = self.iteration_jsonl(number)
        if plain.exists():
            return plain.open("rb")
        archived = plain.with_suffix(".jsonl.gz")
        if archived.exists():
            import gzip

            return gzip.open(archived, "rb")
        raise FileNotFoundError(plain)

    def iteration_prompt(self, number: int) -> Path:
        return self.path / f"iteration-{number}-prompt.md"

    def gate_log(self, number: int) -> Path:
        return self.path / f"gate-{number}.log"

    # -- live status ------------------------------------------------------

    # -- ownership --------------------------------------------------------

    def claim(self) -> None:
        """Record that this process is the loop for this run."""
        try:
            self.pid_path.write_text(f"{os.getpid()}\n")
        except OSError:
            pass  # advisory only; never worth failing a run over

    def release(self) -> None:
        """Drop this process's claim, if the claim is still ours.

        Read straight from the file rather than through `holder`, which reports
        0 for our own pid on purpose so that a loop never blocks itself.
        """
        try:
            mine = int(self.pid_path.read_text().strip()) == os.getpid()
        except (OSError, ValueError):
            return
        if mine:
            self.pid_path.unlink(missing_ok=True)

    def holder(self) -> int:
        """The pid of a live lmloop loop on this run, or 0 -- see `runrecord.holder`.

        This exists because two loops on one run directory is not a harmless
        race.  The dashboard's "continue" spawned a resume beside a loop that
        was merely paused; both then held on the same PAUSE, and had it been
        cleared, both would have run iterations in the same worktree, writing
        the same status.json and committing over each other.

        Reports 0 for our own pid on top of the shared check, so that a loop
        never blocks itself.
        """
        pid = runrecord.holder(self.path)
        return 0 if pid == os.getpid() else pid

    def heartbeat(self) -> None:
        """Restamp `status.json` without otherwise changing it.

        For the pause hold, which has nothing new to say but still has to say
        it: readers judge staleness by how long ago this file moved, so a run
        that is deliberately holding must keep proving it is there.  Missing or
        unreadable is not an error -- the caller is a display loop, and there is
        nothing useful it could do about it.
        """
        try:
            state = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return
        self.write_status(state)

    def write_status(self, state: dict) -> None:
        """Overwrite `status.json` with what the run is doing right now.

        The event log is an append-only history and answering "what is happening
        now" from it means parsing to the end of a file that reaches megabytes.
        This is one small file holding only the present, so a phone script, a
        status bar, or a web wrapper can read it without understanding lmloop.
        Written atomically because something is always reading it.  Also where
        `schema_version` gets stamped -- see `runrecord.py`'s module docstring
        -- since this is the one place every write of this file passes through.
        """
        state = dict(
            state,
            updated_at=datetime.now(timezone.utc).isoformat(),
            schema_version=runrecord.SCHEMA_VERSION,
        )
        temporary = self.status_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(self.status_path)
        except OSError:
            pass  # a status file is never worth failing a run over

    def read_run_state(self) -> dict:
        try:
            value = json.loads(self.run_state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_run_state(self, state: dict) -> None:
        temporary = self.run_state_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(self.run_state_path)
        except OSError:
            pass

    def write_terminal_status(self, reason: str, iteration: int, done: int, total: int) -> None:
        completed = reason.startswith("plan complete")
        try:
            previous = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            previous = {}
        for field in ("eta_seconds", "eta_at", "eta_basis", "eta_samples"):
            previous.pop(field, None)
        self.write_status({
            **previous, "iteration": iteration,
            "phase": "completed" if completed else "stopped",
            "stop_reason": reason, "plan_done": done, "plan_total": total,
            "stopping": False,
        })

    # -- control ----------------------------------------------------------

    def stop_requested(self) -> bool:
        """The run should end -- either at the boundary, or right now."""
        return runrecord.stop_requested(self.path)

    def stop_now_requested(self) -> bool:
        """The in-flight iteration should be cut short rather than finished.

        Two sentinels because the two stops answer different questions.  STOP is
        for a run you are done with: the current iteration finishes, is gated,
        checked, handed off and committed, and only then does the run exit --
        which is the whole value of it, because that is where an hour of work
        becomes a commit and a handoff the next run can start from.  STOP-NOW is
        for an iteration that is visibly wasting its hour, and cannot wait for
        it.  Neither discards anything: the partial tree is committed either way.
        """
        return runrecord.stop_now_requested(self.path)

    def paused(self) -> bool:
        return runrecord.paused(self.path)


def _trim(text: str, limit: int = 88) -> str:
    """Cut at a word boundary; a sentence severed mid-word reads as corruption."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def previous_runs(worktree_root: Path, current_run_id: str, limit: int = 3) -> list[dict]:
    """What earlier runs on this repository attempted, and how they ended.

    A run starts in a fresh worktree and therefore knows nothing about the ones
    before it, so the same repository gets rediscovered from scratch every time
    -- including the dead ends.  The artifacts are already on disk; nothing here
    is new information, it was just never offered to anybody.

    This is deliberately a digest and not the archive.  The full record is 86 MB
    per run, almost all of it raw event stream, and none of that belongs in a
    prompt.  What is worth carrying forward is: what was tried, whether it
    landed, and the one line the agent left about where it got to.
    """
    found = []
    try:
        candidates = sorted(worktree_root.glob("*/.lmloop/runs/*"), reverse=True)
    except OSError:
        return []
    for run_dir in candidates:
        if run_dir.name == current_run_id or not run_dir.is_dir():
            continue
        try:
            objective = (run_dir / "prompt.md").read_text(errors="replace").strip()
        except OSError:
            continue

        commits, iterations, outcomes = 0, 0, []
        try:
            for line in (run_dir / "lmloop.log").read_text(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "iteration:end":
                    iterations += 1
                    outcomes.append(event.get("outcome", ""))
                elif event.get("event") == "run:complete":
                    commits = event.get("commitCount", commits)
        except OSError:
            pass

        handoff = ""
        try:
            lines = (run_dir / "handoff.md").read_text(errors="replace").strip().splitlines()
            handoff = lines[0].strip() if lines else ""
        except OSError:
            pass

        found.append({
            "run_id": run_dir.name,
            "objective": _trim(objective.splitlines()[0]) if objective else run_dir.name,
            "iterations": iterations,
            "commits": commits,
            "outcomes": outcomes,
            "handoff": handoff[:140],
        })
        if len(found) >= limit:
            break
    return found


# Words that start an instruction without describing it.  Only true filler is
# listed: the verb is kept, because "fix the login" and "document the login"
# are different runs and the first word is what says which.
SLUG_FILLER = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "some", "any",
    "i", "we", "you", "id", "ive", "lets", "let", "please",
    "want", "wants", "wanted", "need", "needs", "needed", "like", "would",
    "should", "could", "can", "will", "shall", "to", "for", "of", "and",
    "so", "then", "now", "just", "really", "very", "up",
})

SLUG_MAX = 28


def make_slug(prompt: str) -> str:
    """The readable middle of a run id.

    Two things were wrong with taking the first 24 characters.  It cut inside a
    word -- `make-the-gamble-king-fro` names a directory and a branch that will
    be read a hundred times and says "fro" in the middle of it -- and it spent a
    third of its budget on the words that begin every objective ever written.
    So: drop leading filler, then stop at a word boundary rather than a column.
    """
    import re

    every = [w for w in re.split(r"[^a-z0-9]+", prompt.lower()) if w]
    # Everywhere, not just the front.  Keeping interior filler was the tidier
    # rule grammatically and the worse one in practice: it spent the budget on
    # articles and then stopped on one, so `add-a-test-suite-for-the` ended on
    # the same mid-thought note the column cut was introduced to fix.
    words = [w for w in every if w not in SLUG_FILLER]
    if not words:
        words = every

    kept: list[str] = []
    length = 0
    for word in words:
        addition = len(word) + (1 if kept else 0)
        if kept and length + addition > SLUG_MAX:
            break
        # A single word longer than the whole budget still has to be cut, but
        # only when it would otherwise leave the slug empty.
        kept.append(word if kept or len(word) <= SLUG_MAX else word[:SLUG_MAX])
        length += addition
    return "-".join(kept)


def make_run_id(prompt: str) -> str:
    """``<date>-<slug>-<hash>``.

    the predecessor's hash suffix is a good collision strategy; leading with the date is
    the part it is missing, so a directory of runs sorts chronologically.
    """
    import hashlib

    digest = hashlib.sha256(prompt.encode()).hexdigest()[:6]
    return f"{time.strftime('%Y-%m-%d')}-{make_slug(prompt) or 'run'}-{digest}"
