"""Run one iteration of pi and reduce its event stream to an outcome.

Four things about pi 0.84.2 shape this module, the first three verified against
``~/.local/lib/node_modules/@earendil-works/pi-coding-agent/dist`` and the
fourth observed live:

1. **``--mode json`` always exits 0.**  In ``modes/print-mode.js`` the branch
   that sets ``exitCode = 1`` on ``stopReason === "error" | "aborted"`` sits
   inside ``if (mode === "text")``.  A loop that trusts ``$?`` in JSON mode
   reports success forever.  The outcome comes from the stream or from nowhere.
2. **SIGTERM exits 143 through a real dispose** that calls
   ``killTrackedDetachedChildren()``, so a timeout does not orphan the bash
   commands the agent started.
3. **The stream is enormous.**  One iteration produced a 9.9 MB JSONL, 25k lines
   of which were single-token deltas.  Lines are substring-filtered before they
   reach ``json.loads``; the raw stream is still teed to disk untouched.
4. **pi compacts silently on overflow,** emitting ``compaction_start`` /
   ``compaction_end`` with ``reason: "overflow"``, and the model carries on as if
   nothing happened.  On a 57344-token window that is not a rare event, and an
   agent can spend an entire iteration overflowing: read a dozen files, compact
   to a plan, re-read the same dozen files, compact again.  So the count is
   supervised like the stall clock is, and the summary pi wrote on the way out
   is worth more than anything else the iteration produced -- see
   ``rundir.last_compaction_summary``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# pi's built-in mutating tools, plus `replace` from pi-hashline-edit-pro and the
# names other edit extensions use.  Which one is live depends on what is
# installed in ~/.pi/agent/settings.json, so match them all.
#
# This count is diagnostic only and always undercounts: an agent that appends to
# a file with a bash heredoc changes the tree without touching an edit tool.
# Progress is measured with git, never with this number.
WRITE_TOOLS = {"write", "edit", "replace", "multiedit", "apply_patch"}

# Only these four event types are ever parsed.  Everything else is teed to disk
# and skipped -- see the volume note above.
_INTERESTING = (
    '"tool_execution_start"',
    '"message_end"',
    '"agent_end"',
    '"compaction_start"',
)

# What counts as the model actually doing something, for the stall clock.
# pi writes a session header to stdout the instant it starts, before it has
# contacted a model at all -- so "we have seen output" is not the same as "the
# model is alive", and treating them as the same would start the stall timer
# while llama-swap is still loading weights.  That load legitimately takes
# minutes and emits nothing.
_ACTIVITY = (b'"message_', b'"tool_execution')

TERM_GRACE_SECONDS = 30

# How often the supervisor wakes to check the clocks and refresh the display.
# This is the status line's frame rate as much as it is a timeout granularity:
# at 5s the spinner crawled and the run read as frozen on a phone.  The work per
# tick is a lock, a dict, and one small atomic file write.
POLL_SECONDS = 2


@dataclass
class IterationResult:
    outcome: str  # ok | agent-error | timeout | stalled | thrashing
    detail: str = ""
    tool_calls: int = 0
    writes: int = 0
    compactions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    stderr_tail: str = ""
    files_touched: list[str] = field(default_factory=list)


class _Stream:
    """Shared state between the reader threads and the supervising loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_event_at = 0.0
        self.first_event_at = 0.0
        self.tool_calls = 0
        self.writes = 0
        self.compactions = 0
        self.files: list[str] = []
        self.stop_reason = ""
        self.error_message = ""
        self.saw_message_end = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.stderr = ""
        self.last_tool = ""
        self.last_target = ""

    def note_output(self) -> None:
        """Any byte from pi. Keeps the stall clock fresh once it is running."""
        self.last_event_at = time.monotonic()

    def note_activity(self) -> None:
        """The model is demonstrably alive; the stall clock may now start."""
        now = time.monotonic()
        if not self.first_event_at:
            self.first_event_at = now
        self.last_event_at = now


def _target(args: dict) -> str:
    """What a tool call is pointed at, in a few words.

    "read" tells you the agent is alive; "read players.py" tells you what it is
    doing, which is the difference between a status line worth watching and one
    worth ignoring.  Only the tail of a path is kept -- the worktree prefix is
    the same for every call and would push the useful part off a phone screen.
    """
    for key in ("path", "file_path", "filePath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value.rsplit("/", 1)[-1]
    command = args.get("command")
    if isinstance(command, str) and command:
        return " ".join(command.split())[:60]
    pattern = args.get("pattern") or args.get("query")
    if isinstance(pattern, str) and pattern:
        return pattern[:40]
    return ""


def _handle(event: dict, state: _Stream) -> None:
    kind = event.get("type")
    if kind == "tool_execution_start":
        state.tool_calls += 1
        name = event.get("toolName", "")
        state.last_tool = name
        state.last_target = _target(event.get("args") or {})
        if name in WRITE_TOOLS:
            state.writes += 1
            path = (event.get("args") or {}).get("path")
            if path and path not in state.files:
                state.files.append(path)
    elif kind == "compaction_start":
        state.compactions += 1
    elif kind == "message_end":
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return
        state.saw_message_end = True
        state.stop_reason = message.get("stopReason") or ""
        state.error_message = message.get("errorMessage") or ""
        usage = message.get("usage") or {}
        state.input_tokens = max(state.input_tokens, int(usage.get("input") or 0))
        state.output_tokens += int(usage.get("output") or 0)


def _read_stdout(pipe, raw_path: Path, state: _Stream) -> None:
    buffer = b""
    with raw_path.open("wb") as sink:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            sink.write(chunk)
            sink.flush()
            with state.lock:
                state.note_output()
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if any(marker in line for marker in _ACTIVITY):
                    with state.lock:
                        state.note_activity()
                if not any(marker.encode() in line for marker in _INTERESTING):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                with state.lock:
                    _handle(event, state)


def _read_stderr(pipe, state: _Stream) -> None:
    for chunk in iter(lambda: pipe.read(8192), b""):
        with state.lock:
            state.stderr = (state.stderr + chunk.decode(errors="replace"))[-4000:]


def _terminate(process: subprocess.Popen) -> None:
    """SIGTERM the whole group, then SIGKILL what is left.

    The group matters: pi spawns bash children, and its SIGTERM handler only
    reaps the ones it tracked.
    """
    try:
        group = os.getpgid(process.pid)
    except OSError:
        return
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_SECONDS), (signal.SIGKILL, 5)):
        try:
            os.killpg(group, sig)
        except OSError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run(
    *,
    model: str,
    tools: str,
    thinking: str,
    prompt: str,
    cwd: Path,
    session_dir: Path,
    session_id: str,
    raw_path: Path,
    timeout_seconds: int,
    stall_seconds: int,
    max_compactions: int = 0,
    env: dict | None = None,
    should_stop=lambda: False,
    on_progress=None,
) -> IterationResult:
    argv = [
        "pi",
        "--model", model,
        "--mode", "json",
        "--session-dir", str(session_dir),
        "--session-id", session_id,
    ]
    if tools:
        argv += ["--tools", tools]
    if thinking:
        argv += ["--thinking", thinking]

    started = time.monotonic()
    state = _Stream()
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )

    # The prompt goes in on a thread: it can outgrow the 64K pipe buffer, and a
    # blocking write with nobody draining stdout deadlocks both ends.
    def feed():
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    threads = [
        threading.Thread(target=feed, daemon=True),
        threading.Thread(target=_read_stdout, args=(process.stdout, raw_path, state), daemon=True),
        threading.Thread(target=_read_stderr, args=(process.stderr, state), daemon=True),
    ]
    for thread in threads:
        thread.start()

    killed = ""
    while process.poll() is None:
        time.sleep(POLL_SECONDS)
        now = time.monotonic()
        with state.lock:
            first_event = state.first_event_at
            last_event = state.last_event_at
            writes = state.writes
            compactions = state.compactions
            snapshot = {
                "elapsed": now - started,
                "tool_calls": state.tool_calls,
                "writes": state.writes,
                "compactions": state.compactions,
                "last_tool": state.last_tool,
                "last_target": state.last_target,
                "output_tokens": state.output_tokens,
                # Before the first event this is time spent waiting on
                # llama-swap to load, not the agent going quiet.
                "quiet": (now - last_event) if first_event else 0.0,
                "loading": not first_event,
            }
        if on_progress:
            on_progress(snapshot)

        if now - started > timeout_seconds:
            killed = "timeout"
        elif first_event and now - last_event > stall_seconds:
            # The stall clock only starts once pi has said something.  Before
            # that, llama-swap may legitimately be evicting one model and
            # loading another, which takes minutes and emits nothing.
            killed = "stalled"
        elif max_compactions and compactions >= max_compactions and not writes:
            # Compaction thrash.  Observed on one-project: the agent read 12-16
            # files, overflowed, compacted to a plan, distrusted the plan, and
            # re-read the same files -- six times in 69 minutes, all reads, no
            # writes.  Each summary was larger than the last, so the usable
            # window shrank and the cycle tightened instead of converging.
            #
            # Cutting this off is safe by construction: whatever the iteration
            # left behind is committed either way, so an early cut cannot
            # discard work.  The write counter undercounts -- an agent that
            # appends with a bash heredoc never touches an edit tool -- so this
            # can in principle fire on an agent that did write.  The cost when
            # wrong is one iteration ended early, which the next one resumes
            # from; the cost of not firing is a wasted hour.
            killed = "thrashing"
        elif should_stop():
            killed = "stopped"

        if killed:
            _terminate(process)
            break

    process.wait()
    for thread in threads:
        thread.join(timeout=10)

    elapsed = time.monotonic() - started
    with state.lock:
        if killed == "timeout":
            outcome, detail = "timeout", f"no result after {elapsed / 60:.0f}m"
        elif killed == "stalled":
            outcome, detail = "stalled", f"no output for {stall_seconds // 60}m"
        elif killed == "thrashing":
            outcome = "thrashing"
            detail = f"{state.compactions} context overflows with no writes"
        elif killed == "stopped":
            outcome, detail = "interrupted", "stop requested mid-iteration"
        elif state.stop_reason in ("error", "aborted"):
            outcome = "agent-error"
            detail = state.error_message or f"pi reported {state.stop_reason}"
        elif not state.saw_message_end:
            outcome, detail = "agent-error", "pi produced no assistant message"
        elif state.stop_reason == "length" and not state.writes:
            # The model talked until its output budget ran out and the message
            # ended mid-sentence, so the tool call it was building never
            # arrived.  Seen on both models here: local-wide at 8192 tokens
            # with no tool call at all, and local-fast at 8192 after 45k
            # characters of deliberating over test cases it never wrote.
            # `ok` is the wrong word for it -- nothing was produced, and the
            # fix is a bigger output budget or a lower thinking level, neither
            # of which anyone reaches for while the log says success.
            outcome = "truncated"
            detail = f"ran out of output budget after {state.output_tokens} tokens"
        elif not state.tool_calls:
            # An iteration that ends cleanly having called no tool cannot have
            # changed anything, so "ok" is a lie the run then repeats in the
            # commit log and the notes.  Observed on local-wide: 19 minutes
            # spent drafting the target file inside one reasoning block, the
            # 8192-token output cap reached mid-thought, message over, worktree
            # untouched, outcome recorded as ok.  A reasoning model can think
            # its whole budget away, and that is worth naming.
            outcome = "no-action"
            detail = f"finished without calling a tool ({state.output_tokens} output tokens)"
        else:
            outcome, detail = "ok", state.stop_reason or "completed"

        return IterationResult(
            outcome=outcome,
            detail=detail,
            tool_calls=state.tool_calls,
            writes=state.writes,
            compactions=state.compactions,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            elapsed_seconds=elapsed,
            stderr_tail=state.stderr[-1000:],
            files_touched=list(state.files),
        )
