"""What lmloop needs from an agent, and how each one provides it.

The loop is not really about pi.  Everything that makes it worth having -- the
plan, the handoff, git as the only witness, the structural checks, never
discarding work -- is independent of which agent does the typing.  What is
pi-specific is narrow: an argv, and the shape of the JSON events it streams.

So that is what an adapter is.  Each one answers three questions:

    what command runs an iteration
    which lines are worth parsing at all
    what does this event mean

Everything downstream speaks the vocabulary below, and knows nothing about the
agent that produced it.

Both adapters here were written against captured output, not documentation.
`oh-my-pi` needs no adapter of its own: it is a pi *extension* that pi
auto-discovers, so it arrives through `PiHarness` unchanged.
"""

from __future__ import annotations

# The normalised vocabulary.  An adapter maps its agent's events onto these and
# returns None for everything else.
TOOL = "tool"                 # {kind, name, target}
COMPACTION = "compaction"     # {kind}
MESSAGE_END = "message_end"   # {kind, stop_reason, error, input, output}


def _tail(path: str) -> str:
    """Just the file name.  The worktree prefix is identical on every call and
    would push the useful part off a phone screen."""
    return path.rsplit("/", 1)[-1]


class Harness:
    """The interface. Subclasses are small and should stay that way."""

    name = ""
    binary = ""
    # Substrings that make a line worth `json.loads`.  The streams reach tens of
    # megabytes, almost all single-token deltas, so this filter runs on every
    # line before anything parses it.
    interesting: tuple[str, ...] = ()
    # Byte markers proving the model is alive, for the stall clock.  Before the
    # first one, silence means a model is still loading, not that it has hung.
    activity: tuple[bytes, ...] = ()

    def argv(self, *, model, tools, thinking, session_dir, session_id) -> list[str]:
        raise NotImplementedError

    def classify(self, event: dict) -> dict | None:
        raise NotImplementedError

    def compaction_summary(self, event: dict) -> str:
        """The summary the agent wrote for itself when its context overflowed.

        Empty for agents that do not expose one; the loop then falls back to
        synthesising a handoff from git, which is worse but never wrong.
        """
        return ""


class PiHarness(Harness):
    """pi 0.84.2, and anything layered on it -- including oh-my-pi.

    Three things about pi shape this, all verified against its dist bundle:
    `--mode json` always exits 0 (the branch setting a non-zero code sits inside
    `if (mode === "text")`), SIGTERM disposes tracked children properly, and the
    stream is enormous.
    """

    name = "pi"
    binary = "pi"
    interesting = (
        '"tool_execution_start"', '"message_end"', '"agent_end"', '"compaction_start"',
    )
    activity = (b'"message_', b'"tool_execution')

    def argv(self, *, model, tools, thinking, session_dir, session_id):
        argv = [
            self.binary,
            "--model", model,
            "--mode", "json",
            "--session-dir", str(session_dir),
            "--session-id", session_id,
        ]
        if tools:
            argv += ["--tools", tools]
        if thinking:
            argv += ["--thinking", thinking]
        return argv

    def classify(self, event):
        kind = event.get("type")
        if kind == "tool_execution_start":
            args = event.get("args") or {}
            return {
                "kind": TOOL,
                "name": event.get("toolName", ""),
                "target": self._target(args),
                "path": args.get("path"),
            }
        if kind == "compaction_start":
            return {"kind": COMPACTION}
        if kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                return None
            usage = message.get("usage") or {}
            return {
                "kind": MESSAGE_END,
                "stop_reason": message.get("stopReason") or "",
                "error": message.get("errorMessage") or "",
                "input": int(usage.get("input") or 0),
                "output": int(usage.get("output") or 0),
            }
        return None

    @staticmethod
    def _target(args: dict) -> str:
        for key in ("path", "file_path", "filePath"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return _tail(value)
        command = args.get("command")
        if isinstance(command, str) and command:
            return " ".join(command.split())[:60]
        pattern = args.get("pattern") or args.get("query")
        return pattern[:40] if isinstance(pattern, str) else ""

    def compaction_summary(self, event):
        return ((event.get("result") or {}).get("summary") or "").strip()


class OpencodeHarness(Harness):
    """opencode, via `run --format json`.

    Written against captured output rather than documentation.  Its stream is
    structured quite differently from pi's: events carry a `part`, tool calls
    arrive as one `tool_use` with the result already attached rather than as a
    start/end pair, and token usage rides on `step_finish` instead of a message.

    Two consequences worth knowing.  It exposes no compaction event, so the
    summary harvest is unavailable and an overflowing iteration falls back to a
    git-synthesised handoff.  And a step's `reason` is the closest thing it has
    to a stop reason -- `tool-calls` between steps, `stop` at the end -- so only
    the final one is meaningful.
    """

    name = "opencode"
    binary = "opencode"
    interesting = ('"tool_use"', '"step_finish"')
    activity = (b'"text"', b'"tool_use"', b'"step_')

    def argv(self, *, model, tools, thinking, session_dir, session_id):
        # opencode keeps its own sessions and takes no tool allowlist, so
        # session_dir and tools have nowhere to go.  Saying so is better than
        # passing flags it will reject.
        argv = [self.binary, "run", "--format", "json"]
        if model:
            argv += ["--model", model]
        if thinking:
            argv += ["--variant", thinking]
        return argv

    def classify(self, event):
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "tool_use":
            state = part.get("state") or {}
            return {
                "kind": TOOL,
                "name": part.get("tool", ""),
                "target": self._target(state.get("input") or {}),
                "path": (state.get("input") or {}).get("filePath"),
            }
        if kind == "step_finish":
            tokens = part.get("tokens") or {}
            return {
                "kind": MESSAGE_END,
                "stop_reason": part.get("reason") or "",
                "error": "",
                "input": int(tokens.get("input") or 0),
                "output": int(tokens.get("output") or 0),
            }
        return None

    @staticmethod
    def _target(args: dict) -> str:
        for key in ("filePath", "path", "file"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return _tail(value)
        command = args.get("command")
        if isinstance(command, str) and command:
            return " ".join(command.split())[:60]
        pattern = args.get("pattern") or args.get("query")
        return pattern[:40] if isinstance(pattern, str) else ""


_HARNESSES = {h.name: h for h in (PiHarness(), OpencodeHarness())}


def get(name: str) -> Harness:
    try:
        return _HARNESSES[(name or "pi").strip().lower()]
    except KeyError:
        raise SystemExit(
            f"lmloop: unknown harness {name!r}; known: {', '.join(sorted(_HARNESSES))}"
        ) from None
