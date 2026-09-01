"""Run one agent iteration and reduce its event stream to an outcome.

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

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import harness
import policy
from runner_stream import _Stream, _handle, _read_stderr, _read_stdout

TERM_GRACE_SECONDS = 30

# Supervisor wake/display cadence. Stream-rate windows live in runner_stream.
POLL_SECONDS = 2


@dataclass
class IterationResult:
    outcome: str  # ok | agent-error | provider-unavailable | timeout | stalled | thrashing
    detail: str = ""
    tool_calls: int = 0
    writes: int = 0
    compactions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # The largest single reply, and how many were cut off at the cap.  The cap
    # is per message; `output_tokens` is the whole iteration's total, and the
    # two are not comparable -- see `_Stream`.
    peak_output: int = 0
    truncations: int = 0
    elapsed_seconds: float = 0.0
    stderr_tail: str = ""
    files_touched: list[str] = field(default_factory=list)


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
    tool_seconds: int = 0,
    max_compactions: int = 0,
    max_repeats: int = policy.REPEAT_LIMIT,
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
            tool_started = state.tool_started_at
            repeated = policy.repeating_call(state.signatures, max_repeats) if max_repeats else ""
            state.repeated = repeated
            snapshot = {
                "elapsed": now - started,
                "tool_calls": state.tool_calls,
                "writes": state.writes,
                "compactions": state.compactions,
                "last_tool": state.last_tool,
                "last_target": state.last_target,
                "output_tokens": state.output_tokens,
                "peak_output": state.peak_output,
                "truncations": state.truncations,
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
        elif tool_seconds and tool_started and now - tool_started > tool_seconds:
            # A tool call that has been running this long is not a model
            # thinking, and `stall_seconds` is the wrong clock for it: that one
            # is sized for how long a slow model may take to say anything, and
            # is routinely raised into the hours for exactly that reason.
            #
            # Observed: an agent launched a headless Chrome inside its bash
            # tool for a frontend objective and the browser never exited.  That
            # blocked the tool call, which blocked the agent, which went
            # silent, and the run sat idle for 38 minutes -- until
            # `stall_seconds`, which that repository had set to 3600.
            #
            # Safe by construction, like every other cut here: whatever the
            # iteration wrote is gated, checked and committed on the way out.
            killed = "tool-timeout"
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
        elif max_repeats and repeated:
            # The model going in a circle: the same tool pointed at the same
            # thing, `max_repeats` times, with nothing between that could have
            # changed the answer.  A different failure from thrashing, which is
            # the window losing to the codebase -- this one fits fine and is
            # simply not reading its own results.  Observed on one run: 222
            # tool calls in 1h45m, the same reads cycling, every clock the loop
            # had watching for silence while the agent was busy.
            #
            # Safe by construction like every other cut here: the iteration's
            # work is gated, checked and committed on the way out.
            killed = "looping"
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
        elif killed == "tool-timeout":
            outcome = "tool-timeout"
            what = " ".join(part for part in (state.last_tool, state.last_target) if part)
            detail = (f"{what or 'a tool call'} ran for "
                      f"{tool_seconds // 60}m without returning")
        elif killed == "thrashing":
            outcome = "thrashing"
            detail = f"{state.compactions} context overflows with no writes"
        elif killed == "looping":
            outcome = "looping"
            name, _, _identity = state.repeated.partition("\x00")
            what = " ".join(part for part in (name, state.last_target) if part)
            detail = f"repeated `{what}` {max_repeats}x with nothing between"
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
            # The peak, not the total: what ran out is one message's budget.
            detail = f"ran out of output budget after {state.peak_output} tokens"
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
            peak_output=state.peak_output,
            truncations=state.truncations,
            elapsed_seconds=elapsed,
            stderr_tail=state.stderr[-1000:],
            files_touched=list(state.files),
        )
