"""Pure stop/budget/retry policy, extracted from `loop.Run` per lm-ka5.2.

Everything below is a function of its arguments: no filesystem, no
subprocess, no `self`. `Run` still owns the state these decisions are made
from -- the config, the rundir, the run's own counters -- and gathers it into
plain values before calling in here, the same "inject collaborators" shape
`runrecord.py` established for the run-record contract. That split is what
makes a stop decision testable without a worktree, a harness, or an hour of
wall-clock time actually passing.

`Run._abort_reason`, `Run._budget`, `Run._transport_failure`, and the delay
half of `Run._backoff` keep their exact original call signatures, gather
`self.*` into these functions, and return exactly what they returned before
-- see their docstrings in `loop.py` for the reasoning behind each rule; this
module is where the arithmetic and classification that reasoning produced
actually lives. `Run._backoff` itself stays in `loop.py`: it owns
`self._errors` across calls and makes a real network check
(`_server_is_up`) and a real sleep, neither of which belongs in a pure
function -- only "given this many consecutive failures, how long or give
up" moved here.
"""

from __future__ import annotations

import time

# Outcomes where the agent ran to the end of its turn and produced nothing
# git-visible.  That is what the streak is for: three of these in a row is
# a run going nowhere, and `no-action` -- a clean turn that called no tool
# at all -- is the single clearest example of it.
#
# Everything else is excluded, because it is a failure the loop already
# answers somewhere else and counting it here stops a healthy run for
# something that was never about the work: `agent-error` on a broken
# stream backs off, `thrashing` splits the step, `timeout` and `stalled`
# killed the iteration, `interrupted` was the operator.  One run here
# stopped on "no git-visible change in 3 consecutive iterations" whose
# three iterations were agent-error, interrupted, agent-error -- not one
# completed attempt among them.
NO_PROGRESS_OUTCOMES = frozenset({"ok", "no-action", "truncated"})


def counts_as_no_progress(last_outcome: str, commit: str | None, uncommitted: bool) -> bool:
    return (
        last_outcome in NO_PROGRESS_OUTCOMES
        and commit is None
        and not uncommitted
    )


def budget(
    iteration: int,
    plan_done: int,
    plan_total: int,
    *,
    iteration_floor: int,
    iteration_ceiling: int,
    retry_allowance: int,
) -> int:
    """The budget for a run that follows its plan.

    `Run._budget` returns `iteration_floor` directly, without calling this,
    when the run is not configured to follow its plan at all -- see its
    docstring in `loop.py`. This only has to handle the two shapes that
    remain: no plan yet (`plan_total == 0`), and a plan with some steps left.

    One iteration per step plus `retry_allowance` spare, so a step that needs
    a second attempt does not cost the run its last step. Spent, plus what is
    left, plus slack: a wasted iteration raises the first term without
    lowering the second, so the budget moves out by exactly the one that was
    wasted -- which is the whole request: a step that fails twice must not
    cost the run its last step. Capped by `iteration_ceiling`, which no
    amount of plan growth can argue past, and never below `iteration_floor`.
    """
    if not plan_total:
        # No plan yet.  Deriving a budget from an empty plan would say "one",
        # and stop the run at the planning iteration -- which is what the floor
        # is for.  The ceiling still applies: without it this returned
        # `iteration + 1` every time, so `iteration > max_iterations` could
        # never be true and the turn ceiling was unreachable.  An agent that
        # never wrote a plan then ran until the wall clock stopped it -- ten
        # hours on the shipped default.  A fake-harness run configured for two
        # iterations reached forty-three.
        return max(iteration_floor, min(iteration + 1, iteration_ceiling))
    spent, remaining = iteration - 1, max(plan_total - plan_done, 0)
    wanted = spent + remaining + retry_allowance
    return max(iteration_floor, min(wanted, iteration_ceiling))


def abort_reason(
    iteration: int,
    started: float,
    *,
    interrupted: bool,
    stop_now_requested: bool,
    stop_requested: bool,
    plan_done: int,
    plan_total: int,
    max_iterations: int,
    iteration_ceiling: int,
    elapsed_before: float,
    max_wall_hours: float,
    no_diff_streak: int,
    no_diff_iterations_limit: int,
    plan_at_start: int | None,
) -> str | None:
    """Why this run should stop now, or None to keep going.

    Order matters and is preserved exactly from `Run._abort_reason`:
    interruption and the two control sentinels first, since those are the
    operator overriding everything else; plan completion next, because a run
    that finished what it set out to do has a different story than one that
    ran out of budget; then the iteration ceiling, the wall clock, and
    finally the no-diff streak -- the one guard that cannot be talked out of
    stopping, because it trusts only git, never a self-report.
    """
    if interrupted:
        return "interrupted"
    if stop_now_requested:
        return "STOP-NOW sentinel present"
    if stop_requested:
        return "STOP sentinel present"
    # Before the budget check: a run that has done everything it set out to
    # do has finished, and reporting that as "ran out of iterations" tells
    # the operator to add more of something it does not need.
    if plan_total and plan_done >= plan_total and iteration > 1:
        return f"plan complete ({plan_done}/{plan_total})"
    if iteration > max_iterations:
        if max_iterations >= iteration_ceiling:
            return "turn ceiling hit"
        return f"max iterations reached ({max_iterations})"
    hours = (elapsed_before + time.monotonic() - started) / 3600
    if hours >= max_wall_hours:
        return f"max wall clock reached ({hours:.1f}h)"
    if no_diff_streak >= no_diff_iterations_limit:
        reason = f"no git-visible change in {no_diff_iterations_limit} consecutive iterations"
        if plan_total and plan_at_start is not None and plan_done > plan_at_start:
            reason += (
                f" (plan advanced {plan_at_start}/{plan_total} -> "
                f"{plan_done}/{plan_total}, so this may be steps that need no code)"
            )
        elif plan_total:
            reason += f" (plan still at {plan_done}/{plan_total})"
        return reason
    return None


# What the agent says when the model server went away underneath it, rather
# than when the model did something wrong.  pi retries these itself a few
# times; these are the ones that outlast its retries, which means the server
# was gone for minutes -- a restart, a reload, a swap -- not a blip.
TRANSPORT = (
    "stream ended without finish_reason",
    "connection refused",
    "connection reset",
    "connection error",
    "remote end closed",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    # What pi actually reports when llama-swap stops answering mid-stream.
    # Observed: the server was shut down 23 minutes into an iteration and
    # the agent surfaced "Request timed out." -- which matched none of the
    # phrases above, so the loop recorded a genuine agent-error and charged
    # the run an iteration for a machine that was switched off.
    #
    # Safe as a bare phrase because of what it is tested against: lmloop's
    # own clocks produce the outcomes `timeout` and `stalled`, never
    # `agent-error`, so a timeout reported *inside* an agent-error is the
    # agent timing out on the model -- which is the transport, by
    # definition.
    "timed out",
    # llama-swap attaches this to a temporarily poisoned upstream after a
    # context overflow.  The observed failure said reset after 30s, then
    # returned the same stale token-count error with a shrinking
    # retry-after-ms on six fresh two-message requests.  Treating each as a
    # model failure burned six iterations in 26 seconds.  This marker means
    # exactly what the transport asks us to do: back off and retry.
    "retry-after-ms=",
    "no route to host",
    "name or service not known",
)


def transport_failure(outcome: str, commit: str | None, detail: str | None) -> str:
    """The detail, if this iteration died of the server rather than itself.

    An iteration that ends this way has produced nothing and learned
    nothing, and charging it against `max_iterations` spends one of a very
    small number on an event that had nothing to do with the work.

    Only when it left no commit.  If the agent got far enough to change
    files, the iteration is worth keeping whatever killed it, and redoing it
    would mean redoing work that is already in git.
    """
    if outcome != "agent-error" or commit:
        return ""
    lowered = (detail or "").lower()
    return detail if any(marker in lowered for marker in TRANSPORT) else ""


def backoff_delay(error_count: int) -> int | None:
    """Seconds to wait before retrying after a server-side failure that was
    not "the server is not there" (that case waits indefinitely instead --
    see `Run._wait_for_server`), or None to give up.

    `error_count` is the number of consecutive such failures including this
    one -- `Run._backoff` increments `self._errors` before calling this, and
    resets it to 0 when the server is confirmed back, so three separate
    outages across one run never combine into a false give-up.

    1m, 2m, 4m, then give up: a bad build or a model stuck failing every
    request is not worth six hours against, unlike a genuinely absent server.
    """
    if error_count > 3:
        return None
    return 60 * 2 ** (error_count - 1)


def failure_reason(error: BaseException) -> str:
    """How a run that died of an exception should describe itself afterwards.

    The distinction that matters to whoever reads the run back is operator
    versus defect: a second Ctrl-C is somebody taking their terminal back and
    is not a crash, so it reads as `interrupted` and lands in the same
    vocabulary as every other deliberate stop.  Anything else names its type,
    because "crashed" on its own sends a reader to the event log to find out
    what actually happened.

    Capped, because this string is written into `status.json` as
    `stop_reason` and printed in the closing summary, and an exception
    carrying a whole subprocess transcript in its message would drown both.
    """
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    detail = " ".join(str(error).split())
    named = f"crashed: {type(error).__name__}" + (f": {detail}" if detail else "")
    return named[:197] + "..." if len(named) > 200 else named
