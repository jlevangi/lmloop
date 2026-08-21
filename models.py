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

# How a real window is split, and where llama-swap is.  One file, because this
# was three copies: here, in `lmloop models --detect`, and in the pi extension
# that actually configures the agent -- and they had already drifted, this side
# reserving the default 8192 for local-wide where the extension reserves 24576, so
# lmloop believed in 16K of prompt room pi had never been given.
#
# The extension reads the same file.  Both sides keep working defaults, so a
# missing or unparseable file changes nothing; it is a place to edit, not a
# dependency.
BUDGETS_FILE = Path.home() / ".config" / "lmloop" / "model-budgets.json"

# pi's own provider config.  The authority for models lmloop does not measure
# itself -- see `declared_window`.
PI_MODELS_FILE = Path.home() / ".pi" / "agent" / "models.json"

_FALLBACK = {
    # opencode built a 68,194-token request against a correctly declared 65,536:
    # a system prompt and tool definitions escape whatever budget an agent
    # compacts to.  Declaring real-minus-this keeps the request inside it.
    "headroom": 8192,
    # HEADROOM assumes the prompt runs out first, which is backwards for a
    # reasoning model.  An override replaces headroom on *both* sides, so the
    # split still lands exactly on the real window.
    "output_override": {"local-wide": 24576, "local-fast": 16384},
    "unmeasured_context": 24576,
    "llama_swap_url": "http://127.0.0.1:8080",
}


def budgets() -> dict:
    """The split policy, from the shared file, falling back to the defaults."""
    try:
        loaded = json.loads(BUDGETS_FILE.read_text())
    except (OSError, ValueError):
        return dict(_FALLBACK)
    # Keys prefixed with `_` are prose for whoever opens the file.
    merged = dict(_FALLBACK)
    merged.update({k: v for k, v in loaded.items() if not k.startswith("_")})
    return merged


# Kept as module attributes because callers and tests read them by name; both
# now come from `budgets()` rather than being written down a second time.
HEADROOM = budgets()["headroom"]
OUTPUT_OVERRIDE = budgets()["output_override"]

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


def _pi_declared(provider: str, model_id: str) -> tuple[int, int] | None:
    """A window pi has been told about, from pi's own `models.json`.

    For anything lmloop cannot measure itself -- 9router, or any cloud provider
    -- that file is the authority, because it is the same file pi reads when it
    builds the request.  Consulting it here means lmloop and pi agree by
    construction rather than by someone remembering to edit two places.

    These are *declared* windows, not measured ones, and the difference is the
    whole reason self-hosted models do not come through here: a router reports
    what a model advertises, not how the weights were loaded.  See the module
    docstring, and the three runs that cost.
    """
    try:
        config = json.loads(PI_MODELS_FILE.read_text())
    except (OSError, ValueError):
        return None
    entries = ((config.get("providers") or {}).get(provider) or {}).get("models") or []
    for entry in entries:
        if entry.get("id") != model_id:
            continue
        context, output = entry.get("contextWindow"), entry.get("maxTokens")
        if isinstance(context, int) and isinstance(output, int):
            return context, output
    return None


def declared_window(model: str) -> tuple[int, int] | None:
    """``(context, max_output)`` we believe is safe, or None if unknown."""
    if "/" not in model:
        return None
    provider, name = model.split("/", 1)

    if provider != "llama-swap":
        # Not measurable from here, so pi's own config is the best answer there
        # is.  Returning None -- which is what this did -- made every 9router
        # model look like it had no context at all: `Run.window` fell to 0, the
        # dashboard's gauge went blank, and the thrash escalation, which picks a
        # rescue model by comparing windows, ranked them below everything and so
        # could never choose one.
        return _pi_declared(provider, name)

    real = load_cache().get(name)
    if not real:
        return None
    # Step for step with the extension: `reserved = override ?? headroom`,
    # `contextWindow = max(real - reserved, 8192)`,
    # `maxTokens = override ?? min(headroom, contextWindow / 4)`.
    policy = budgets()
    override = (policy["output_override"] or {}).get(name)
    headroom = policy["headroom"]
    reserved = override if override is not None else headroom
    context = max(real - reserved, 8192)
    return context, override if override is not None else min(headroom, context // 4)
