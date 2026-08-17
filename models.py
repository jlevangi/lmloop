"""Model resolution, preflight, and the context-window cache.

Two facts drive everything here.

**llama-swap holds one model at a time.**  Asking for a model that is not loaded
evicts the one that is, and the request blocks for the swap.  So a health check
must never name a model: ``GET /running`` is free and tells you what is loaded,
while ``GET /upstream/<model>/props`` *causes* the swap it was meant to observe.
A short-timeout probe against that endpoint once stalled a live run and made a
healthy machine look down.

**A router reports model metadata, not how the weights were loaded.**  9router
advertised 1,000,000 context for a model running with ``--ctx-size 65536`` and
262,144 for one running with 98,304.  Declaring those numbers killed runs on
HTTP 400 mid-iteration.  The authority is the llama-server command line in
``/running``, and the cache written from it lives here rather than in a sibling
project's deploy directory.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

CONTEXT_CACHE = Path.home() / ".config" / "lmloop" / "model-context.json"

# opencode built a 68,194-token request against a correctly declared 65,536: a
# system prompt and tool definitions escape whatever budget an agent compacts
# to.  Declaring real-minus-this keeps the request inside the real window.
HEADROOM = 8192

_CTX_SIZE = re.compile(r"--ctx-size\s+(\d+)")


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode())


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def running(base_url: str, timeout: float = 10.0) -> list[dict]:
    """What llama-swap currently holds.  Free; never triggers a swap."""
    return _get(f"{base_url}/running", timeout).get("running", [])


def preflight(model: str, base_url: str) -> tuple[bool, str]:
    """Check the model is reachable.  Returns ``(ok, detail)``.

    For a router-backed model there is nothing cheap to check, so we do not
    check: a dead router surfaces as an ``agent-error`` iteration, which commits
    and hands off like any other outcome.
    """
    if provider_of(model) != "llama-swap":
        return True, "no preflight for non-local models"

    name = model.split("/", 1)[1]
    try:
        entries = running(base_url)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return False, f"llama-swap unreachable at {base_url}: {error}"

    loaded = [entry.get("model") for entry in entries if entry.get("state") == "ready"]
    if name in loaded:
        return True, f"{name} already loaded"
    if loaded:
        # Not an error.  The first request will evict and load, which costs
        # minutes -- which is why the stall timer does not start until the
        # first event arrives.
        return True, f"{name} not loaded ({loaded[0]} is); first request will swap"
    return True, f"{name} not loaded; first request will load it"


def real_context(base_url: str) -> dict[str, int]:
    """Measure the loaded model's true context from the llama-server argv."""
    measured = {}
    for entry in running(base_url):
        name = entry.get("model")
        match = _CTX_SIZE.search(entry.get("cmd", ""))
        if name and match:
            measured[name] = int(match.group(1))
    return measured


def load_cache() -> dict[str, int]:
    try:
        return json.loads(CONTEXT_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(measured: dict[str, int]) -> None:
    CONTEXT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    merged = load_cache() | measured
    CONTEXT_CACHE.write_text(json.dumps(dict(sorted(merged.items())), indent=2) + "\n")


def declared_window(model: str) -> tuple[int, int] | None:
    """``(context, max_output)`` we believe is safe, or None if unmeasured."""
    if provider_of(model) != "llama-swap":
        return None
    real = load_cache().get(model.split("/", 1)[1])
    if not real:
        return None
    context = max(real - HEADROOM, 8192)
    return context, min(HEADROOM, context // 4)
