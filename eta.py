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
            if advanced and seconds > 0:
                samples.extend([seconds / advanced] * advanced)
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
    # Once the active turn runs past its historical allowance, disappearing is
    # the least honest ETA: it looks like the feature broke exactly when the run
    # became late.  One minute means "due now" at this UI's minute precision.
    seconds_left = max(round(seconds_each * remaining - max(elapsed_seconds, 0)), 60)
    completion = datetime.now(timezone.utc) + timedelta(seconds=seconds_left)
    return {
        "eta_seconds": seconds_left,
        "eta_at": completion.isoformat(),
        "eta_basis": basis,
        "eta_samples": len(samples),
    }
