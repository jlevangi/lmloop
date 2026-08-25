"""Conservative completion estimates from a run's durable lifecycle events."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone


USEFUL_OUTCOMES = frozenset({"ok", "truncated"})


def estimate(events: list[dict], *, elapsed_seconds: float = 0,
             iteration: int = 0, max_iterations: int = 0,
             plan_done: int = 0, plan_total: int = 0) -> dict:
    """Return an ETA only after two comparable completed attempts exist.

    Plan-aware estimates use turns that actually advanced the plan.  Before a
    plan exists, clean completed turns are the best available proxy.  Median is
    deliberate: local-model cold loads and overflow-heavy turns are long-tailed.
    """
    ends = [event for event in events if event.get("event") == "iteration:end"]
    samples: list[float] = []
    remaining = 0
    basis = ""

    if plan_total:
        previous_done = 0
        for event in ends:
            done = int(event.get("planDone") or 0)
            advanced = max(done - previous_done, 0)
            previous_done = max(previous_done, done)
            seconds = float(event.get("elapsedMs") or 0) / 1000
            if advanced and seconds > 0 and event.get("outcome") in USEFUL_OUTCOMES:
                # One sample per completed attempt, even when that attempt checks
                # off several steps.  Duplicating by `advanced` would let one
                # iteration satisfy the two-attempt confidence threshold.
                samples.append(seconds / advanced)
        remaining = max(plan_total - plan_done, 0)
        basis = "plan steps"
    else:
        for event in ends:
            seconds = float(event.get("elapsedMs") or 0) / 1000
            if seconds > 0 and event.get("outcome") in USEFUL_OUTCOMES:
                samples.append(seconds)
        # The current iteration is still outstanding.  At an iteration boundary
        # elapsed_seconds is zero, so this remains the number of turns left.
        remaining = max(max_iterations - max(iteration, 1) + 1, 0)
        basis = "iterations"

    if len(samples) < 2 or remaining <= 0:
        return {}

    seconds_each = statistics.median(samples)
    if plan_total:
        # The active step is censored evidence: it has taken at least this long,
        # even though its final duration is not known yet.  A median of a tiny,
        # skewed sample otherwise keeps saying "10m per step" while the current
        # step has visibly been running for an hour.  Use the more conservative
        # of that median and the productive mean including the active lower
        # bound. Failed attempts never enter `samples` above.
        productive_mean = (
            sum(samples) + max(elapsed_seconds, 0)
        ) / (len(samples) + 1)
        seconds_each = max(seconds_each, productive_mean)
    # Once the active turn runs past its historical allowance, disappearing is
    # the least honest ETA: it looks like the feature broke exactly when the run
    # became late.  One minute means "due now" at this UI's minute precision.
    # `remaining` includes the step being worked right now.  Elapsed time may
    # consume that step's allowance, but never the allowance of later steps: a
    # single 70-minute outlier cannot make eleven untouched steps look free.
    current_left = max(seconds_each - max(elapsed_seconds, 0), 0)
    future_left = seconds_each * max(remaining - 1, 0)
    seconds_left = max(round(current_left + future_left), 60)
    completion = datetime.now(timezone.utc) + timedelta(seconds=seconds_left)
    return {
        "eta_seconds": seconds_left,
        "eta_at": completion.isoformat(),
        "eta_basis": basis,
        "eta_samples": len(samples),
    }
