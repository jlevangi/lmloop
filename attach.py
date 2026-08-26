"""Watch a detached run from a terminal, and drive its controls.

`lmloop run --detach` starts a run in its own session and prints a log path,
after which the only views were `tail -f` and polling `lmloop status`. This is
the screen back: the same moving status line a foreground run draws, the
lifecycle events as they happen, and the same `p`/`r`/`q`/`Q` keys.

It works because the controls were already files. Nothing here talks to the
loop process -- it reads `status.json` and `lmloop.log`, and writes the same
`PAUSE`/`STOP`/`STOP-NOW` sentinels the keys have always written. So attaching
is safe from anywhere, including a second terminal while the first is watching,
and detaching is just stopping reading.

**A viewer never owns the run.** It does not claim it, does not write
`status.json`, and Ctrl-C detaches rather than stopping anything -- which is
the one thing that would make this dangerous rather than useful, since the
whole point is to look at a run somebody deliberately left running.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import display
import eta
import runrecord

POLL_SECONDS = 1.0
# Long enough that a slow model between events does not read as dead, short
# enough to notice a crashed loop while you are still looking at it.  Same
# threshold the dashboard and `lmloop status` use, for the same reason.
STALE_AFTER = runrecord.STALE_AFTER_SECONDS


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _line(status: dict, spinner: str, age: float | None, paint) -> list[tuple[int, str]]:
    """The status line, in the same shape and priority order `Run._show` uses.

    Built from `status.json` rather than shared with `_show`, which reads live
    counters the loop holds in memory and this process cannot see. The segment
    weights are copied deliberately: the two screens are supposed to look the
    same, and a viewer that laid them out differently would be worse than no
    viewer at all.
    """
    iteration = status.get("iteration") or 0
    total = status.get("max_iterations") or 0
    stale = age is not None and age > STALE_AFTER

    if stale:
        detail = paint.red(f"no update for {display.elapsed(age)}")
    elif status.get("phase") == "loading":
        detail = paint.yellow("loading model")
    elif status.get("last_tool"):
        target = status.get("last_target") or ""
        detail = paint.cyan(status["last_tool"] + (f" {target}" if target else ""))
    else:
        detail = paint.dim("thinking")

    plan_done = status.get("plan_done") or 0
    plan_total = status.get("plan_total") or 0
    flags = "".join(
        flag for flag, on in (("PAUSE", status.get("paused")),
                              ("STOP", status.get("stopping"))) if on
    )
    rate = status.get("tokens_per_second") or 0
    return [
        (6, f"{paint.cyan(spinner)} {paint.bold(f'{iteration}/{total}')}"),
        (4, display.elapsed(status.get("elapsed_seconds") or 0)),
        (5, detail),
        (3, paint.red(f"{status['compactions']} overflow") if status.get("compactions") else ""),
        (4, paint.green(f"{plan_done}/{plan_total} steps") if plan_total else ""),
        (2, paint.dim(f"{status.get('tool_calls') or 0} tools")),
        (2, paint.dim(f"{rate:.1f} tok/s") if rate else ""),
        (1, paint.dim(f"{status.get('output_tokens') or 0} out")),
        (7, paint.red(f"[{flags}]") if flags else ""),
    ]


def _describe(event: dict) -> str:
    """One line for an event worth interrupting the status line for.

    Only the lifecycle ones. The log also carries per-iteration detail that is
    interesting when reading a finished run and noise when watching a live one.
    """
    name = event.get("event")
    if name == "iteration:start":
        return f"  iteration {event.get('iteration')}"
    if name == "iteration:end":
        outcome = event.get("outcome", "?")
        commit = (event.get("commit") or "")[:8]
        return (f"    {outcome} | {event.get('toolCalls', 0)} tool calls | "
                + (f"committed {commit}" if commit else "nothing to commit"))
    if name == "git:commit:blocked":
        return f"    gate {event.get('gate')} blocked the commit"
    if name == "server:wait":
        return f"    model server is not there; holding ({event.get('detail')})"
    if name == "server:back":
        return f"    model server is back after {display.elapsed(event.get('waited', 0))}"
    if name == "backoff:start":
        return f"    {event.get('detail')}; retrying in {event.get('seconds', 0) // 60}m"
    if name == "run:complete":
        return f"\n  run stopped: {event.get('status')}"
    return ""


def watch(run_dir: Path, run_id: str, screen, keys=None) -> int:
    """Follow a run until it finishes or the terminal goes away.

    Returns 0 when the run ended while attached, 1 when it was already over or
    has gone stale -- so a script can tell "I watched it finish" from "there was
    nothing to watch", which are different answers to the same command.
    """
    status_path = run_dir / "status.json"
    # Start from the end of the log, not the beginning.  Attaching is "show me
    # what happens from now"; replaying forty iterations of a run somebody has
    # been watching for hours is not a view of it, and the history is what
    # `notes.md` and the log itself are for.
    events_seen = len(runrecord.read_events(run_dir))
    finished = False

    opening = _read(status_path)
    if opening:
        screen.log(f"  iteration {opening.get('iteration')}"
                   f"/{opening.get('max_iterations')}, "
                   f"{opening.get('phase')}, "
                   f"{(opening.get('elapsed_seconds') or 0) // 60}m into this one")

    if keys is not None:
        keys.start()
        if screen.tty:
            screen.log(f"  {display.Keys.HELP}")
            screen.log("  ctrl-c detaches; the run keeps going")

    previous_updated = None
    while True:
        status = _read(status_path)
        if not status:
            screen.log(f"  {run_id}: no status yet")
            return 1

        events = runrecord.read_events(run_dir)
        for event in events[events_seen:]:
            described = _describe(event)
            if described:
                screen.log(described)
            if event.get("event") == "run:complete":
                finished = True
        events_seen = len(events)

        if finished:
            screen.close()
            return 0
        if status.get("phase") in ("completed", "stopped"):
            # Already over when we got here.  Different answer from watching
            # one end, so a script can tell them apart.
            screen.close()
            screen.log(f"  already {status['phase']}: {status.get('stop_reason', '')}".rstrip(": "))
            return 1

        age = runrecord.age_seconds(status.get("updated_at"))
        # The spinner advances only when the run has actually written
        # something since the last look, so a frozen loop freezes it rather
        # than being animated over by a viewer that is merely still running.
        moved = status.get("updated_at") != previous_updated
        previous_updated = status.get("updated_at")
        screen.status(_line(status, screen.spin(advance=moved), age, screen.paint))

        time.sleep(POLL_SECONDS)


def eta_for(run_dir: Path, status: dict) -> dict:
    """Rebuild the estimate from the log, for a run whose loop predates it."""
    return eta.estimate(
        runrecord.read_events(run_dir),
        elapsed_seconds=status.get("elapsed_seconds") or 0,
        iteration=status.get("iteration") or 0,
        max_iterations=status.get("max_iterations") or 0,
        plan_done=status.get("plan_done") or 0,
        plan_total=status.get("plan_total") or 0,
    )
