"""Pure stop/budget/retry policy, extracted from `loop.Run` per lm-ka5.2.

Everything below is a function of its arguments: no filesystem, no
subprocess, no `self`. `Run` still owns the state these decisions are made
from -- the config, the rundir, the run's own counters -- and gathers it into
plain values before calling in here, the same "inject collaborators" shape
`runrecord.py` established for the run-record contract. That split is what
makes a stop decision testable without a worktree, a harness, or an hour of
wall-clock time actually passing.

`Run._abort_reason` and `Run._budget` keep their exact original call
signatures (`(iteration, started)` and `(iteration)`), gather `self.*` into
these functions, and return exactly what they return -- see their docstrings
in `loop.py` for the reasoning behind each rule; this module is where the
arithmetic that reasoning produced actually lives.
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
        # and stop the run at the planning iteration.
        return max(iteration_floor, iteration + 1)
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
