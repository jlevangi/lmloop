"""Streaming event reduction for one agent iteration.

Kept separate from process supervision: this module understands event bytes and
normalized harness notes; pi_runner owns subprocess lifetime and stop clocks.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from harness_base import COMPACTION, MESSAGE_END, TOOL, TOOL_END

# pi's built-in mutating tools, plus `replace` from pi-hashline-edit-pro and the
# names other edit extensions use.  Which one is live depends on what is
# installed in ~/.pi/agent/settings.json, so match them all.
#
# This count is diagnostic only and always undercounts: an agent that appends to
# a file with a bash heredoc changes the tree without touching an edit tool.
# Progress is measured with git, never with this number.
WRITE_TOOLS = {"write", "edit", "replace", "multiedit", "apply_patch"}


# How far back `_Stream.rate` falls back to looking, when the live stream has
# gone quiet.  Long enough to span a couple of messages on a 2 tok/s model.
RATE_WINDOW_SECONDS = 300

# The live rate's window.  Short, because this one answers "how fast is it
# generating *now*" and a minute-long average of that is a different question.
LIVE_RATE_WINDOW_SECONDS = 30

# How often the streaming counter records a mark.  Deltas arrive ~50/s; a mark
# per delta would be 1500 tuples a window for no extra precision.
STREAM_MARK_SECONDS = 0.5

# Matched against raw bytes, deliberately.  `harness.interesting` filters the
# stream before anything parses it because the deltas are most of tens of
# megabytes; counting them must stay on that side of the filter.  The trailing
# quote is what keeps this off `"thinking_delta_signature"` and friends.
DELTA_MARKER = b'_delta"'


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
        # Every tool call this iteration, as `name\x00target`, newest last.
        # Even pathological iterations produce only a few KB of signatures;
        # retaining all of them keeps arbitrary `max_repeats` values honest.
        self.signatures: list[str] = []
        self.repeated = ""
        self.stop_reason = ""
        self.error_message = ""
        self.saw_message_end = False
        self.input_tokens = 0
        # Cumulative across every message in the iteration.  `input_tokens`
        # above is a running *maximum* for a reason -- the window is a property
        # of one prompt -- and the output cap is per message the same way, so
        # comparing this total against it is comparing two different things.
        # The number that can be compared is the peak below.
        self.output_tokens = 0
        self.peak_output = 0
        # Messages that ended because they hit the output cap.  Only the *last*
        # message's `stop_reason` survives on this object, so without a count
        # here a reply cut off mid-iteration leaves no trace at all: the agent
        # recovers with something smaller, the iteration ends `ok`, and the one
        # fix that would have helped -- a bigger cap or a lower thinking level
        # -- is never reached for, because nothing said the model ran out of
        # room.
        self.truncations = 0
        self.stderr = ""
        self.last_tool = ""
        self.last_target = ""
        # When the tool call currently in flight started, or 0.0 if none is.
        # A hung tool call and a thinking model look identical from outside --
        # both are silence -- and this is the one thing that tells them apart.
        # See `tool_seconds` in `run`.
        self.tool_started_at = 0.0
        # (monotonic, cumulative output tokens) at each message end.  Output
        # tokens only become known in lumps -- one lump per assistant message,
        # which on a slow model is minutes apart -- so a rate needs the times
        # those lumps landed, not a difference against a fixed start.
        self.token_marks: list[tuple[float, int]] = []
        # (monotonic, cumulative streamed deltas).  The live counterpart to
        # token_marks: a delta is one token off the model *now*, where a
        # message end is a lump that only lands when the whole message is done.
        self.stream_marks: list[tuple[float, int]] = []
        self.streamed = 0
        self.last_rate = 0.0

    def rate(self) -> float:
        """Output tokens per second: the speed of the thing generating now.

        Measured from the delta stream rather than from message ends, and that
        is the whole point.  Output tokens are only *credited* at a message end,
        so a model part-way through a long reply has a frozen numerator and a
        ticking denominator -- the displayed speed does not merely read low, it
        visibly decays.  Raising thinking to high made that the normal case: one
        message can reason for minutes, and the run looked like it was dying
        when it was working hardest.  Worse, an iteration whose message never
        ended was credited nothing at all, so the iterations that most needed
        diagnosing reported the least.

        A delta is one token, near enough -- measured at 0.85-0.98 of the
        reported total across twenty-two real iterations, so this reads a few
        percent low.  That is not calibrated out: a fudge factor fitted to one
        model on one box would be a lie everywhere else, and a number that is
        5% shy beats one that is 8x wrong.

        Between messages, and while a tool runs, the last live figure is held
        rather than recomputed.  Nothing is generating then, so there is no new
        speed to report, and decaying toward zero would recreate the bug.
        """
        now = time.monotonic()
        live = [m for m in self.stream_marks if m[0] >= now - LIVE_RATE_WINDOW_SECONDS]
        if len(live) >= 2:
            span = live[-1][0] - live[0][0]
            if span > 0:
                self.last_rate = (live[-1][1] - live[0][1]) / span
                return self.last_rate
        if self.last_rate:
            return self.last_rate
        # Nothing has streamed yet this iteration -- the model is still on the
        # prompt.  The message-end marks are all there is.
        marks = [m for m in self.token_marks if m[0] >= now - RATE_WINDOW_SECONDS]
        if len(marks) >= 2:
            span = marks[-1][0] - marks[0][0]
            if span > 0:
                return (marks[-1][1] - marks[0][1]) / span
        span = now - self.first_event_at
        return self.output_tokens / span if self.first_event_at and span > 0 else 0.0

    def note_stream(self, count: int) -> None:
        """`count` deltas arrived: the model is producing tokens right now."""
        self.streamed += count
        now = time.monotonic()
        if not self.stream_marks or now - self.stream_marks[-1][0] >= STREAM_MARK_SECONDS:
            self.stream_marks.append((now, self.streamed))
            del self.stream_marks[:-256]

    def note_output(self) -> None:
        """Any byte from pi. Keeps the stall clock fresh once it is running."""
        self.last_event_at = time.monotonic()

    def note_activity(self) -> None:
        """The model is demonstrably alive; the stall clock may now start."""
        now = time.monotonic()
        if not self.first_event_at:
            self.first_event_at = now
        self.last_event_at = now


def _handle(event: dict, state: _Stream, agent) -> None:
    """Fold one event into the run state, in the adapter's normalised terms.

    Nothing below knows which agent produced the event -- see `harness.py`.
    """
    note = agent.classify(event)
    if not note:
        return
    kind = note["kind"]
    if kind == TOOL:
        state.tool_calls += 1
        state.last_tool = note["name"]
        state.last_target = note["target"]
        identity = note.get("identity", note["target"])
        state.signatures.append(f"{note['name']}\x00{identity}")
        # Only for an agent that will also say when the call finished.  For
        # one that does not, every completed call would look like a call still
        # running, and `tool_seconds` would fire on a healthy iteration.
        if agent.reports_tool_ends:
            state.tool_started_at = time.monotonic()
        if note["name"] in WRITE_TOOLS:
            state.writes += 1
            path = note.get("path")
            if path and path not in state.files:
                state.files.append(path)
    elif kind == TOOL_END:
        state.tool_started_at = 0.0
    elif kind == COMPACTION:
        state.compactions += 1
    elif kind == MESSAGE_END:
        state.saw_message_end = True
        state.stop_reason = note["stop_reason"]
        state.error_message = note["error"]
        state.input_tokens = max(state.input_tokens, note["input"])
        state.output_tokens += note["output"]
        state.peak_output = max(state.peak_output, note["output"])
        if note["stop_reason"] == "length":
            state.truncations += 1
        state.token_marks.append((time.monotonic(), state.output_tokens))
        del state.token_marks[:-64]


def _read_stdout(pipe, raw_path: Path, state: _Stream, agent) -> None:
    buffer = b""
    with raw_path.open("wb") as sink:
        while True:
            # read1, not read: `read` waits for the whole 65536 bytes before
            # returning, so on a slow model the stall clock only ticks once a
            # full buffer has accumulated -- roughly every two minutes at the
            # ~570 B/s a 2 tok/s model produces.  The clock is supposed to mean
            # "pi has said nothing at all", so it has to see bytes when they
            # arrive, not when they amount to 64KB.
            chunk = pipe.read1(65536)
            if not chunk:
                break
            sink.write(chunk)
            sink.flush()
            with state.lock:
                state.note_output()
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            # Counted on complete lines, never on the raw chunk: a 64KB read
            # splits a marker across the boundary roughly every chunk, and a
            # counter that silently drops one token per chunk is the kind of
            # thing nobody notices until they are debugging something else.
            deltas = 0
            for line in lines:
                if DELTA_MARKER in line:
                    deltas += 1
                if any(marker in line for marker in agent.activity):
                    with state.lock:
                        state.note_activity()
                if not any(marker.encode() in line for marker in agent.interesting):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                with state.lock:
                    _handle(event, state, agent)
            # One lock acquisition per chunk rather than per delta.
            if deltas:
                with state.lock:
                    state.note_stream(deltas)


def _read_stderr(pipe, state: _Stream) -> None:
    for chunk in iter(lambda: pipe.read(8192), b""):
        with state.lock:
            state.stderr = (state.stderr + chunk.decode(errors="replace"))[-4000:]
