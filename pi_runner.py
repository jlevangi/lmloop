"""Run one iteration of pi and reduce its event stream to an outcome.

Three things about pi 0.84.2 shape this module, all verified against
``~/.local/lib/node_modules/@earendil-works/pi-coding-agent/dist``:

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

# Only these three event types are ever parsed.  Everything else is teed to disk
# and skipped -- see the volume note above.
_INTERESTING = ('"tool_execution_start"', '"message_end"', '"agent_end"')

# What counts as the model actually doing something, for the stall clock.
# pi writes a session header to stdout the instant it starts, before it has
# contacted a model at all -- so "we have seen output" is not the same as "the
# model is alive", and treating them as the same would start the stall timer
# while llama-swap is still loading weights.  That load legitimately takes
# minutes and emits nothing.
_ACTIVITY = (b'"message_', b'"tool_execution')

TERM_GRACE_SECONDS = 30
POLL_SECONDS = 5


@dataclass
class IterationResult:
    outcome: str  # ok | agent-error | timeout | stalled
    detail: str = ""
    tool_calls: int = 0
    writes: int = 0
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
        self.files: list[str] = []
        self.stop_reason = ""
        self.error_message = ""
        self.saw_message_end = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.stderr = ""

    def note_output(self) -> None:
        """Any byte from pi. Keeps the stall clock fresh once it is running."""
        self.last_event_at = time.monotonic()

    def note_activity(self) -> None:
        """The model is demonstrably alive; the stall clock may now start."""
        now = time.monotonic()
        if not self.first_event_at:
            self.first_event_at = now
        self.last_event_at = now


def _handle(event: dict, state: _Stream) -> None:
    kind = event.get("type")
    if kind == "tool_execution_start":
        state.tool_calls += 1
        name = event.get("toolName", "")
        if name in WRITE_TOOLS:
            state.writes += 1
            path = (event.get("args") or {}).get("path")
            if path and path not in state.files:
                state.files.append(path)
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
    prompt: str,
    cwd: Path,
    session_dir: Path,
    session_id: str,
    raw_path: Path,
    timeout_seconds: int,
    stall_seconds: int,
    env: dict | None = None,
    should_stop=lambda: False,
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

        if now - started > timeout_seconds:
            killed = "timeout"
        elif first_event and now - last_event > stall_seconds:
            # The stall clock only starts once pi has said something.  Before
            # that, llama-swap may legitimately be evicting one model and
            # loading another, which takes minutes and emits nothing.
            killed = "stalled"
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
        elif killed == "stopped":
            outcome, detail = "interrupted", "stop requested mid-iteration"
        elif state.stop_reason in ("error", "aborted"):
            outcome = "agent-error"
            detail = state.error_message or f"pi reported {state.stop_reason}"
        elif not state.saw_message_end:
            outcome, detail = "agent-error", "pi produced no assistant message"
        else:
            outcome, detail = "ok", state.stop_reason or "completed"

        return IterationResult(
            outcome=outcome,
            detail=detail,
            tool_calls=state.tool_calls,
            writes=state.writes,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            elapsed_seconds=elapsed,
            stderr_tail=state.stderr[-1000:],
            files_touched=list(state.files),
        )
