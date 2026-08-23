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

Every adapter here was written against captured output, not documentation.

One naming trap is worth stating once, because two different projects answer to
`oh-my-pi`.  The npm package of that name is a pi *extension* -- pi discovers it
and it arrives through `PiHarness` needing no adapter at all.  `OmpHarness`
below is the other one: `github.com/can1357/oh-my-pi`, whose binary is `omp`, a
fork of pi rather than a layer on it.  It has its own binary, its own `~/.omp`,
and enough divergence in its argv and its stream to need an adapter of its own.
"""

from __future__ import annotations

import re

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
    # The line carrying the summary an agent wrote for itself on the way out of
    # a context overflow.  `rundir` scans a whole iteration stream for this
    # before parsing anything, so it is bytes, and it belongs to the adapter:
    # omp names that event `auto_compaction_end` where pi names it
    # `compaction_end`, and a marker hardcoded to either harvests nothing from
    # the other and says nothing about having failed.  Empty means the agent
    # does not compact.
    compaction_marker: bytes = b""
    # The event announcing that overflow, by the name this agent gives it.
    # Paired with `compaction_marker` above and separate from it because one is
    # matched against raw bytes before parsing and the other against a parsed
    # event; keeping both on the adapter is what stops an agent recognising a
    # name only its sibling ever emits.
    compaction_event: str = ""
    # The `--tools` names this agent will accept, when it is fussy about it.
    # Empty means it takes whatever it is given; see `unknown_tools`.
    known_tools: frozenset[str] = frozenset()

    def argv(self, *, model, tools, thinking, session_dir, session_id) -> list[str]:
        raise NotImplementedError

    def classify(self, event: dict) -> dict | None:
        raise NotImplementedError

    def unknown_tools(self, tools: str) -> list[str]:
        """Names in a `tools` string this agent has never heard of.

        The allowlist is the one setting an operator carries over verbatim when
        they change agents, and it is the one that does not carry: pi's default
        names `replace` and `ls`, and omp rejects both -- `CliUsageError:
        Unknown tools in --tools`, exit 1, before a single event.  Answering
        this here lets the loop say so while it is still reading config, rather
        than after it has built a worktree for a run that cannot start.
        """
        if not self.known_tools:
            return []
        wanted = [name.strip() for name in tools.split(",") if name.strip()]
        return [name for name in wanted if name not in self.known_tools]

    def compaction_summary(self, event: dict) -> str:
        """The summary the agent wrote for itself when its context overflowed.

        Empty for agents that do not expose one; the loop then falls back to
        synthesising a handoff from git, which is worse but never wrong.
        """
        return ""


class PiHarness(Harness):
    """pi 0.84.2, and any extension layered on it -- the npm `oh-my-pi` included.

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
    compaction_marker = b'"compaction_end"'
    compaction_event = "compaction_start"

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
                "path": self._path(args),
            }
        if self.compaction_event and kind == self.compaction_event:
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

    @staticmethod
    def _path(args: dict) -> str | None:
        """The file a write tool is about to change, for `files_touched`.

        Split out from `_target` because the two answer different questions:
        one is a label short enough for a phone screen, this is a path the
        checks can look up.  pi puts it in `path`; omp's editor does not have
        one at all, which is what this hook exists for.
        """
        value = args.get("path")
        return value if isinstance(value, str) and value else None

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


# The section header omp's editor puts at the top of every hunk: `[path#TAG]`,
# where TAG is a four-hex digest of the file as the last `read` returned it.
# It is the only place an `edit` call names the file it is editing.
_HASHLINE_SECTION = re.compile(r"\[([^\[\]\n]+)#[0-9A-Fa-f]{4}\]")

# omp's built-in tool names, from `--tools` rejecting everything else.  `search`
# and `find` are its documented aliases for `grep` and `glob` and are accepted
# too.  Kept here rather than in config so that a bad allowlist is caught by the
# adapter that knows, not by a list someone has to remember to update twice.
OMP_TOOLS = frozenset("""
    read bash edit ast_grep ast_edit ask debug eval github glob grep lsp
    inspect_image browser computer checkpoint rewind security_scan task hub
    todo web_search write memory_edit retain recall reflect learn manage_skill
    search find
""".split())

# What to hand `--tools` when a project has not said.  omp's editor is a patch
# language that refuses to touch a file it has not just read, and it cannot
# create one, so `read` and `write` are not optional next to `edit`.  `replace`,
# `ls` and pi's other extension tools are absent because omp has never heard of
# them and exits 1 rather than ignoring them.
OMP_DEFAULT_TOOLS = "read,write,edit,bash,grep,glob"

# The allowlist for work with a user interface in it.  `browser` is omp's own
# tool -- a real Chromium tab over CDP -- so a UI task needs no wrapper script
# and no extension.  Note what is missing: `write`.  This is the set for
# changing an interface that already exists, and adding `write` to create new
# files is a deliberate widening, not an oversight.  See docs/operations.md.
OMP_UI_TOOLS = "read,edit,grep,glob,bash,browser"


class OmpHarness(PiHarness):
    """oh-my-pi (`omp`) -- github.com/can1357/oh-my-pi, captured from v17.4.0.

    A fork of pi rather than an extension of it, so most of `PiHarness` is
    simply right: the event envelope, the `message_end` shape, `stopReason`,
    `usage.input`/`usage.output`, and the fact that `--mode json` reports the
    outcome through the stream or not at all.  Four things are not, and each was
    found by running `omp -p --mode json` against a stub provider rather than by
    reading anything:

    1. **There is no `--session-id`.**  `omp --session-id <uuid>` exits 2 with
       "unknown flag"; sessions are keyed off `--session-dir`, `--continue` and
       `--resume`.  The loop's per-iteration id therefore has nowhere to go, and
       that is fine -- every iteration is a fresh session by design, and the
       handoff file is what carries state between them.

    2. **Print mode is opt-in.**  `--mode json` alone does turn interactivity
       off, but only as a side effect of the mode being set at all; `-p` is the
       documented way to say it and costs nothing to state.

    3. **Compaction is `auto_compaction_start` / `auto_compaction_end`.**  pi's
       names are a prefix short of these, so pi's markers match neither -- an
       omp iteration that overflowed would have looked like one that never
       compacted, and its summary, which is the best thing such an iteration
       produces, would have been dropped for a git diff of nothing.

    4. **The editor is a patch language.**  `edit` takes one string, `input`,
       holding line-anchored hunks under `[path#TAG]` section headers -- there
       is no `path` argument to read.  So the file being edited has to be parsed
       out of the script, and an `edit` whose header is malformed contributes no
       path at all rather than a wrong one.

    One more thing is a policy rather than a fact.  `tools.approvalMode`
    defaults to `yolo`, but it is a *user setting*: an operator who has set it
    to `always-ask` for their interactive omp would get, from lmloop, an
    iteration that blocks on a prompt nobody will ever see, until the stall
    clock kills it twenty minutes later -- every iteration, identically.  So the
    argv states it.  A loop nobody is watching cannot be asked, and a mode that
    hangs is not a safeguard.
    """

    name = "omp"
    binary = "omp"
    interesting = (
        '"tool_execution_start"', '"message_end"', '"agent_end"',
        '"auto_compaction_start"',
    )
    activity = (b'"message_', b'"tool_execution')
    compaction_marker = b'"auto_compaction_end"'
    compaction_event = "auto_compaction_start"
    known_tools = OMP_TOOLS

    def argv(self, *, model, tools, thinking, session_dir, session_id):
        # `session_id` is accepted and dropped; see 1. above.
        argv = [
            self.binary,
            "-p",
            "--mode", "json",
            "--session-dir", str(session_dir),
            "--approval-mode", "yolo",
        ]
        if model:
            argv += ["--model", model]
        if tools:
            argv += ["--tools", tools]
        if thinking:
            argv += ["--thinking", thinking]
        return argv

    @staticmethod
    def _edit_path(args: dict) -> str:
        """The file named by the first section header of an `edit` script."""
        script = args.get("input")
        if not isinstance(script, str):
            return ""
        found = _HASHLINE_SECTION.search(script)
        return found.group(1).strip() if found else ""

    @classmethod
    def _target(cls, args: dict) -> str:
        # `path` first, because read, write, grep and glob all use it -- glob
        # puts its pattern there, which `_tail` shortens to the interesting end
        # of it.  Then the editor, then bash, then a bare pattern.
        value = args.get("path")
        if isinstance(value, str) and value:
            return _tail(value)
        edited = cls._edit_path(args)
        if edited:
            return _tail(edited)
        command = args.get("command")
        if isinstance(command, str) and command:
            return " ".join(command.split())[:60]
        # `url` is the browser tool: the page is what that call is about, and
        # the host is the part of it that survives a narrow terminal.
        url = args.get("url")
        if isinstance(url, str) and url:
            return url[:60]
        pattern = args.get("pattern") or args.get("query")
        return pattern[:40] if isinstance(pattern, str) else ""

    @classmethod
    def _path(cls, args: dict) -> str | None:
        value = args.get("path")
        if isinstance(value, str) and value:
            return value
        return cls._edit_path(args) or None

    def compaction_summary(self, event):
        """omp reports an aborted compaction rather than omitting the event.

        `auto_compaction_end` carries `aborted` and `willRetry` alongside its
        result, and a compaction that gave up has no summary worth carrying --
        harvesting the empty one would overwrite a real handoff with nothing.
        """
        if event.get("aborted") or event.get("skipped"):
            return ""
        return super().compaction_summary(event)


_HARNESSES = {h.name: h for h in (PiHarness(), OmpHarness(), OpencodeHarness())}


def get(name: str) -> Harness:
    try:
        return _HARNESSES[(name or "pi").strip().lower()]
    except KeyError:
        raise SystemExit(
            f"lmloop: unknown harness {name!r}; known: {', '.join(sorted(_HARNESSES))}"
        ) from None
