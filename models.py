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
    # Model-id prefixes that mean "this is served by the local llama-swap,
    # whatever the agent calls it".  Everything in this module that reads
    # `/running`, parses a `--ctx-size`, or trusts the context cache applies to
    # these prefixes and nothing else.  What follows the prefix is the bare
    # model name llama-swap knows it by, which is what the measured cache is
    # keyed on.
    #
    # A LIST, because one server arrives under as many names as there are ways
    # to reach it.  Measured on one machine, all three of these are the same
    # weights on the same box:
    #
    #     pi        llama-swap/Qwen3.8-27B                 (direct)
    #     omp       9router/pc-llama-swap/Qwen3.8-27B      (through a router)
    #     opencode  9router/pc-llama-swap/Qwen3.8-27B      (through a router)
    #
    # and only the first was recognised.  The other two were treated as remote,
    # so lmloop believed what the router advertised -- 262144 for a model
    # actually loaded with `--ctx-size 131072`, which is the exact failure this
    # module's docstring exists to describe.  Declaring the prefix is how an
    # operator says "these two names are my server too".
    #
    # Empty list turns the local path off entirely: no preflight, no cache
    # lookup, every model's metadata from the agent's own catalogue.  That is
    # the supported configuration for a machine with no local server, and it is
    # why nothing here compares a provider to a literal.
    "local_providers": ["llama-swap"],
}


# What `config.load` last layered on top of the shared file, for this process.
# See `use` below.
_OVERRIDES: dict = {}


def use(settings: dict) -> None:
    """Layer a config's `[models]` section over the shared budgets file.

    `model-budgets.json` is shared with the pi extension on purpose -- both
    sides have to split a window the same way or lmloop believes in prompt room
    pi was never given -- so it stays the place those numbers live.  But a
    *project* may still need to say something different, and before this the
    only settings reachable from `.lmloop.toml` were the ones `config.DEFAULTS`
    happened to copy out at import time.

    `config.load` calls this with the fully layered `[models]` section, which is
    the one place that knows what defaults, global config and the repo's own
    file add up to.  Process-wide because the alternative is threading a config
    through `declared_window`, `preflight` and `is_local` to reach the two
    functions that actually read policy -- and every caller of those already
    ran `config.load` first.
    """
    _OVERRIDES.clear()
    _OVERRIDES.update({k: v for k, v in settings.items() if k in _FALLBACK})


def forget_overrides() -> None:
    """Drop them again.  For tests, and for anything loading a second config."""
    _OVERRIDES.clear()


def budgets() -> dict:
    """The split policy: defaults, then the shared file, then this config."""
    merged = dict(_FALLBACK)
    try:
        loaded = json.loads(BUDGETS_FILE.read_text())
    except (OSError, ValueError):
        loaded = {}
    # Keys prefixed with `_` are prose for whoever opens the file.
    merged.update({k: v for k, v in loaded.items() if not k.startswith("_")})
    merged.update(_OVERRIDES)
    return merged


# Kept as module attributes because callers and tests read them by name; both
# now come from `budgets()` rather than being written down a second time.
HEADROOM = budgets()["headroom"]
OUTPUT_OVERRIDE = budgets()["output_override"]


def local_providers() -> list[str]:
    """Model-id prefixes served by the local server; empty if there is none.

    Accepts the older single-string `local_provider` key as well, so a budgets
    file written before this was a list keeps working unchanged.
    """
    policy = budgets()
    configured = policy.get("local_providers")
    if configured is None:
        configured = policy.get("local_provider") or []
    if isinstance(configured, str):
        configured = [configured] if configured else []
    return [prefix for prefix in configured if prefix]


def local_provider() -> str:
    """The first configured local prefix, or "".

    Kept for the callers that need one name to build an id with rather than a
    set to test against -- `lmloop models` printing what it just measured.
    """
    prefixes = local_providers()
    return prefixes[0] if prefixes else ""


def local_name(model: str) -> str:
    """The bare name the local server knows this model by, or "".

    The measured cache is keyed on what `GET /running` reports, which is the
    model name alone -- so a router-qualified id has to have the whole prefix
    taken off, not just the first path segment.
    """
    for prefix in local_providers():
        if model.startswith(prefix + "/"):
            return model[len(prefix) + 1:]
    return ""


def is_local(model: str) -> bool:
    """Is this a model whose real window we can measure, rather than be told?"""
    return bool(local_name(model))

_CTX_SIZE = re.compile(r"--ctx-size\s+(\d+)")


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode())


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def running(base_url: str, timeout: float = 10.0) -> list[dict]:
    """What llama-swap currently holds.  Free; never triggers a swap."""
    return _get(f"{base_url}/running", timeout).get("running", [])


def available(base_url: str, timeout: float = 10.0) -> list[str]:
    """Every model this server can serve, loaded or not.

    Free, like `/running` and unlike `/upstream/<model>/props`: the entries come
    back with a `status` of `unloaded` rather than being loaded to answer.  So
    this can tell "a name this server has never heard of" from "a name it will
    load on first use", which `/running` alone cannot.
    """
    payload = _get(f"{base_url}/v1/models", timeout)
    return [entry.get("id", "") for entry in payload.get("data", []) if entry.get("id")]


def preflight(model: str, base_url: str) -> tuple[bool, str]:
    """Check the model is reachable.  Returns ``(ok, detail)``.

    For a router-backed model there is nothing cheap to check, so we do not
    check: a dead router surfaces as an ``agent-error`` iteration, which commits
    and hands off like any other outcome.
    """
    if not is_local(model):
        return True, "no preflight for non-local models"

    name = local_name(model)
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


_HARNESS_WINDOWS: dict[str, dict[str, tuple[int, int]]] = {}


def harness_windows(harness_name: str = "") -> dict[str, tuple[int, int]]:
    """The agent's own catalogue, fetched at most once per process.

    Cached because an adapter is allowed to be slow: pi reads a file, but omp
    is asked -- `omp models --json`, about two seconds -- and `Run._wider_model`
    walks every candidate model looking for a wider window.
    """
    import harness  # local: harness imports nothing from here, and this keeps it that way

    if harness_name not in _HARNESS_WINDOWS:
        try:
            _HARNESS_WINDOWS[harness_name] = harness.get(harness_name).declared_windows()
        except SystemExit:
            _HARNESS_WINDOWS[harness_name] = {}   # unknown agent: no catalogue
    return _HARNESS_WINDOWS[harness_name]


def forget_harness_windows() -> None:
    """Drop the cached catalogues.  For tests, and for `lmloop models`."""
    _HARNESS_WINDOWS.clear()


def declared_window(model: str, harness_name: str = "") -> tuple[int, int] | None:
    """``(context, max_output)`` we believe is safe, or None if unknown."""
    if "/" not in model:
        return None
    if not is_local(model):
        # Not measurable from here, so the agent's own catalogue is the best
        # answer there is -- and it has to be *this* agent's.  Returning None
        # made every router model look like it had no context at all:
        # `Run.window` fell to 0, the dashboard's gauge went blank, and the
        # thrash escalation, which picks a rescue model by comparing windows,
        # ranked them below everything and so could never choose one.  Reading
        # pi's catalogue whatever the agent did the same thing to omp, which
        # keeps its own and knows far more models than pi's file lists.
        return harness_windows(harness_name).get(model)

    name = local_name(model)
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
