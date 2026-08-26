"""Tell someone when a run has finished.

A run is hours long and unattended by design, which means the moment it ends is
the moment nobody is looking.  Everything else in this project exists to make a
finished run legible after the fact; this exists so the fact reaches you at all.

Deliberately one push at the end rather than progress updates.  A notification
per iteration would be a notification every twenty minutes for ten hours, and a
channel that cries wolf that often stops being read -- which would cost more than
it gives, since the whole point is the one message that matters.

stdlib only, and it can never fail a run: a dead ntfy server, a typo in a URL
and a network outage all end the same way, with an event in the log and the run
finishing normally.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import config

TIMEOUT_SECONDS = 10


def _headers(title: str, tags: str, priority: str, click: str, token: str) -> dict:
    headers = {
        # ntfy reads its metadata from headers, and they must be latin-1 safe:
        # an objective with an em dash in it would otherwise raise on encode and
        # take the notification with it.
        "Title": title.encode("ascii", "replace").decode(),
        "Tags": tags,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def summarise(run: dict) -> tuple[str, str, str, str]:
    """(title, body, tags, priority) for a finished run.

    The title has to carry the verdict on its own: it is what appears on a lock
    screen, and "my-app: 9 commits" answers the question that made someone
    start the run, where "run complete" does not.
    """
    repo = run.get("repo", "lmloop")
    commits = run.get("commits", 0)
    plan_done, plan_total = run.get("plan", (0, 0))

    if commits:
        title = f"{repo}: {commits} commit{'s' if commits != 1 else ''}"
        tags, priority = "crescent_moon", "default"
    else:
        # No commits is the case worth waking up for: the run spent hours and
        # git has nothing to show for it.
        title = f"{repo}: nothing committed"
        tags, priority = "warning", "high"

    iterations = run.get("iterations", 0)
    lines = [run.get("objective", "")[:180]]
    detail = [f"{iterations} iteration{'s' if iterations != 1 else ''}",
              f"{run.get('hours', 0):.1f}h"]
    if plan_total:
        detail.append(f"plan {plan_done}/{plan_total}")
    lines.append(" · ".join(detail))
    lines.append(f"stopped: {run.get('reason', 'complete')}")

    failures = run.get("failures") or {}
    if failures:
        lines.append("failed: " + ", ".join(f"{n}x {name}" for name, n in failures.items()))
    if run.get("defects"):
        lines.append(f"{len(run['defects'])} file(s) left broken")

    return title, "\n".join(line for line in lines if line), tags, priority


def send(settings: dict, run: dict) -> str:
    """Push one notification.  Returns "" on success, or the reason it failed."""
    base = (settings.get("url") or "").strip().rstrip("/")
    topic = (settings.get("topic") or "").strip().strip("/")
    if not base:
        return "no url configured"
    # Either a bare server plus a topic, or a URL that already names one.
    target = f"{base}/{topic}" if topic else base

    title, body, tags, priority = summarise(run)
    click = settings.get("dashboard_url") or ""
    if click and run.get("project") and run.get("run_id"):
        click = f"{click.rstrip('/')}/#{run['project']}/{run['run_id']}"

    request = urllib.request.Request(
        target,
        data=body.encode("utf-8"),
        # Resolved here rather than at load: a config may point at the token
        # instead of holding it (`env:`, `file:`, `!command` -- see
        # `config.secret`), and resolving early would put the real value into
        # every config dict the process passes around, including the ones the
        # dashboard renders.
        headers=_headers(title, tags, priority, click,
                         config.secret(settings.get("token", ""))),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status >= 300:
                return f"HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return str(error)
    return ""
