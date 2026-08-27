"""Who to Web Push, and where that list lives.

A flat list of browser `PushSubscription` objects, persisted to a JSON file
under the dashboard's config directory. Deduped by `endpoint`: a push
endpoint URL is unique per browser profile and origin, so re-subscribing (a
permission re-grant, a new device) replaces the previous entry for that
browser rather than accumulating stale ones that just fail silently forever.

This is the first web-side file written by more than one request at a time --
`status.json` (see `rundir.py:write_status`) only ever has one writer, the
loop process. `ThreadingHTTPServer` serves concurrently, so the read-modify-
write here is additionally guarded by a process-wide lock; the write itself
uses the same `.json.tmp` -> `.replace()` atomic-write idiom used everywhere
else state is persisted in this project.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()


def default_path() -> Path:
    override = os.environ.get("LMLOOP_WEB_PUSH_STORE", "")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "lmloop" / "push_subscriptions.json"


def _read(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def _write(path: Path, subscriptions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(subscriptions, indent=2) + "\n")
    temporary.replace(path)


def add(path: Path, subscription: dict) -> None:
    """Store one subscription, replacing any existing entry with the same
    endpoint."""
    endpoint = subscription.get("endpoint", "")
    if not endpoint:
        return
    with _LOCK:
        subscriptions = [s for s in _read(path) if s.get("endpoint") != endpoint]
        subscriptions.append(subscription)
        _write(path, subscriptions)


def remove(path: Path, endpoint: str) -> None:
    if not endpoint:
        return
    with _LOCK:
        subscriptions = [s for s in _read(path) if s.get("endpoint") != endpoint]
        _write(path, subscriptions)


def all_subscriptions(path: Path) -> list[dict]:
    with _LOCK:
        return _read(path)
