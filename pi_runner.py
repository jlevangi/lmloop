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

import harness

# pi's built-in mutating tools, plus `replace` from pi-hashline-edit-pro and the
# names other edit extensions use.  Which one is live depends on what is
# installed in ~/.pi/agent/settings.json, so match them all.
#
# This count is diagnostic only and always undercounts: an agent that appends to
# a file with a bash heredoc changes the tree without touching an edit tool.
# Progress is measured with git, never with this number.
WRITE_TOOLS = {"write", "edit", "replace", "multiedit", "apply_patch"}

TERM_GRACE_SECONDS = 30

# How often the supervisor wakes to check the clocks and refresh the display.
# This is the status line's frame rate as much as it is a timeout granularity:
# at 5s the spinner crawled and the run read as frozen on a phone.  The work per
# tick is a lock, a dict, and one small atomic file write.
POLL_SECONDS = 2

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
    if kind == harness.TOOL:
        state.tool_calls += 1
        state.last_tool = note["name"]
        state.last_target = note["target"]
        if note["name"] in WRITE_TOOLS:
            state.writes += 1
            path = note.get("path")
            if path and path not in state.files:
                state.files.append(path)
    elif kind == harness.COMPACTION:
        state.compactions += 1
    elif kind == harness.MESSAGE_END:
        state.saw_message_end = True
        state.stop_reason = note["stop_reason"]
        state.error_message = note["error"]
        state.input_tokens = max(state.input_tokens, note["input"])
        state.output_tokens += note["output"]
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
    agent_name: str = "pi",
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
    agent = harness.get(agent_name)
    argv = agent.argv(
        model=model, tools=tools, thinking=thinking,
        session_dir=session_dir, session_id=session_id,
    )

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
        threading.Thread(target=_read_stdout, args=(process.stdout, raw_path, state, agent), daemon=True),
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
                # The prompt as the model actually counted it, which is the only
                # honest measure of how close this iteration is to the window it
                # will compact at.
                "input_tokens": state.input_tokens,
                "tokens_per_second": state.rate(),
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
            # Compaction thrash.  Observed on one project: the agent read 12-16
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
