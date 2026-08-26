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

import json
import re
import subprocess
from pathlib import Path

# The normalised vocabulary.  An adapter maps its agent's events onto these and
# returns None for everything else.
TOOL = "tool"                 # {kind, name, target}
COMPACTION = "compaction"     # {kind}
MESSAGE_END = "message_end"   # {kind, stop_reason, error, input, output}
# A tool call finishing.  Paired with TOOL so the loop can tell "a tool call is
# still running" from "the model is thinking", which look identical from
# outside: both are silence.  An agent that reports a tool call and its result
# as one event has no in-flight state to report and never emits this.
TOOL_END = "tool_end"         # {kind}


# A provider name as an agent prints it: a bare token in the first column.
# Header and rule lines from a table do not match, and neither does a model id
# that already carries its provider.
_PROVIDER = re.compile(r"[A-Za-z0-9][\w.-]*")


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
    # Where this agent keeps its own configuration, if lmloop knows.  Used to
    # report what is loaded into it -- extensions in particular, which are
    # invisible from here and can change what a run is allowed to do.
    config_dir: Path | None = None
    # The tool allowlist to use when the operator never chose one.  Empty means
    # "the shipped default in `config.DEFAULTS` already suits this agent" --
    # true for pi, whose names that default was written from, and for opencode,
    # which takes no allowlist at all.  omp rejects names it does not have
    # rather than ignoring them, so it needs its own; see `unknown_tools`.
    default_tools: str = ""
    # The name of this agent's browser tool, if it has one.  Empty means it does
    # not, and `Run.probe_browser` then has nothing to preflight.  A name rather
    # than a flag because the preflight also has to find it in the allowlist.
    browser_tool: str = ""
    # Does this agent announce a tool call starting and finishing separately?
    # Only then can the loop tell "a tool call is still running" from "the
    # model is thinking" and cut a hung subprocess short -- see `tool_seconds`.
    # An agent that reports a call and its result as one event has no in-flight
    # state to offer, and the check has to stay off for it rather than treat
    # every completed call as one that never returned.
    reports_tool_ends: bool = True
    # Environment variables this agent needs, on top of `env.BASE_ALLOW`.
    # Trailing `*` is a prefix.  The adapter owns these because nothing else
    # can: `PI_CODING_AGENT_DIR` relocates pi's whole config directory and is
    # meaningless to opencode, and a list kept anywhere else would have to know
    # every agent's private namespace.  Credential-shaped names still need an
    # explicit `[env] pass` entry -- a prefix here is not an opt-in for one.
    env_passthrough: tuple[str, ...] = ()

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

    def list_models_argv(self) -> list[str]:
        """How to ask this agent what models it can reach, or `[]` if it cannot.

        `lmloop models` used to shell out to `pi --list-models` whatever agent
        was configured, so an omp or opencode setup was shown pi's catalogue --
        or pi's error, on a machine where pi is not installed at all.

        This is the question asked *for a person*: what it runs prints a table
        meant for eyes, and `lmloop models` passes it through untouched.  For
        the same catalogue as data, see `catalogue`.  Which of the two an agent
        can answer is one capability, so `[]` here means it can answer neither.
        """
        return []

    def catalogue(self) -> list[str]:
        """Every model selector this agent will accept, asked of the agent.

        The same question as `list_models_argv`, in a form that can be parsed.
        Separate because the printable answer is not the parseable one: omp's
        is a box-drawing table, and reading it with the column parser pi's
        output invites yields its two provider headers -- `9router/(97)` and
        `llama-swap/(7)`, both offered by the dashboard as models, neither of
        which exists.  Which is the failure the whole thing exists to prevent:
        offering a model the agent cannot resolve produces a run that dies on
        its first request, minutes later, for a reason nobody can see from
        where they picked it.

        `OSError`, `ValueError` and `SubprocessError` are deliberately not
        caught here.  "The agent could not be run" and "the agent knows no
        models" are different answers, and only the caller knows how to say
        either one.
        """
        return []

    def declared_windows(self) -> dict[str, tuple[int, int]]:
        """`(context, max_output)` per full model selector, from this agent's
        own catalogue.  Empty when the agent cannot be asked.

        For any model lmloop cannot measure itself -- a router, a cloud
        provider -- the agent's catalogue is the authority, because it is what
        the agent will actually build its request against.  It has to come from
        the adapter because each agent keeps its own: pi reads
        `~/.pi/agent/models.json`, omp has a separate config directory
        (`~/.omp/agent`) and a much larger catalogue, and asking pi's file on
        omp's behalf answered for four models out of ninety-seven.

        Callers cache this -- see `models.declared_window`.  An implementation
        is allowed to be slow.
        """
        return {}

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
    # `PI_CODING_AGENT_DIR` is the one that matters: it relocates pi's whole
    # config directory -- models.json, sessions, settings -- and is how a run
    # is pointed at a scratch config instead of the operator's own.
    env_passthrough = ("PI_*",)

    def list_models_argv(self):
        return [self.binary, "--list-models"]

    def catalogue(self):
        result = subprocess.run(
            self.list_models_argv(), capture_output=True, text=True, timeout=60
        )
        return self.parse_catalogue(result.stdout)

    @classmethod
    def parse_catalogue(cls, stdout: str) -> list[str]:
        """`provider model ...` columns, one model per line.

        Lived in `web/server.py` as the parser for every agent, which is how
        omp's table came back as two models named after its provider counts.
        Here it answers for pi, and for the fork that prints what pi prints.
        """
        models = []
        for line in stdout.splitlines():
            parts = line.split()
            # Any provider, not a list of the two this was first deployed
            # against.  `9router` is one person's router and had no business
            # being a condition in shipped code; a provider name is whatever
            # the agent says it is.  Which leaves the table's own header to
            # exclude, since `provider model` is otherwise shaped exactly like
            # a row -- excluded by what it says rather than by guessing at
            # column widths, so a change in the layout costs nothing and a
            # change in the wording costs one model.
            if parts[:2] == ["provider", "model"]:
                continue
            if len(parts) >= 2 and _PROVIDER.fullmatch(parts[0]):
                models.append(f"{parts[0]}/{parts[1]}")
        return models

    # pi's own provider config: the authority for models lmloop does not
    # measure itself, because it is the same file pi reads when it builds the
    # request.
    config_dir = Path.home() / ".pi" / "agent"
    models_file = config_dir / "models.json"

    def declared_windows(self):
        try:
            config = json.loads(self.models_file.read_text())
        except (OSError, ValueError):
            return {}
        windows = {}
        for provider, section in (config.get("providers") or {}).items():
            for entry in (section or {}).get("models") or []:
                model_id = entry.get("id")
                context, output = entry.get("contextWindow"), entry.get("maxTokens")
                if model_id and isinstance(context, int) and isinstance(output, int):
                    windows[f"{provider}/{model_id}"] = (context, output)
        return windows
    interesting = (
        '"tool_execution_start"', '"tool_execution_end"', '"message_end"',
        '"agent_end"', '"compaction_start"',
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
        if kind == "tool_execution_end":
            return {"kind": TOOL_END}
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
    # Its tool events arrive with the result already attached -- one `tool_use`
    # per call, never a start and an end -- so a call is never observably in
    # flight and `tool_seconds` cannot apply.
    reports_tool_ends = False
    env_passthrough = ("OPENCODE_*",)
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
    # omp is a pi fork and reads pi's variables too, so this adds to the
    # inherited `PI_*` rather than replacing it.
    env_passthrough = ("PI_*", "OMP_*")
    config_dir = Path.home() / ".omp" / "agent"
    interesting = (
        '"tool_execution_start"', '"tool_execution_end"', '"message_end"',
        '"agent_end"', '"auto_compaction_start"',
    )
    activity = (b'"message_', b'"tool_execution')
    compaction_marker = b'"auto_compaction_end"'
    compaction_event = "auto_compaction_start"
    known_tools = OMP_TOOLS
    default_tools = OMP_DEFAULT_TOOLS
    browser_tool = "browser"

    def list_models_argv(self):
        # Not pi's `--list-models`, which omp rejects outright: `Error: unknown
        # flag: --list-models`.  It has a `models` subcommand instead.  Verified
        # against omp v17.4.0 -- inheriting pi's spelling printed that error
        # where a catalogue belonged.
        #
        # What this prints is a box-drawing table, which is why `catalogue`
        # below does not read it; see the note there.
        return [self.binary, "models"]

    def _models_json(self) -> list[dict]:
        """omp's catalogue as data.

        `omp models` prints a provider header and then a box-drawing table --
        for eyes, not for parsing.  `--json` is the same catalogue in a form
        that can be read, and it also avoids parsing the `models.yml` behind
        `~/.omp/agent/models.db`, which this project has no YAML dependency
        for.

        Roughly two seconds, so both callers below are cached by whoever calls
        them.  Errors are left to those callers, which want different things
        from a failure.
        """
        result = subprocess.run(
            [self.binary, "models", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        catalogue = json.loads(result.stdout)
        if not isinstance(catalogue, dict):
            return []
        return [entry for entry in catalogue.get("models") or []
                if isinstance(entry, dict)]

    def catalogue(self):
        """The `selector` of every model omp knows -- `provider/id`, which is
        exactly the string `--model` takes.

        Not `parse_catalogue`, which reads pi's columns: run over `omp models`,
        the only lines shaped like a row are the two provider headers, whose
        second token is a count.  That produced `9router/(97)` and
        `llama-swap/(7)`, offered by the dashboard as the entire catalogue of
        an agent that knows ninety-seven models.
        """
        return [
            entry["selector"] for entry in self._models_json()
            if isinstance(entry.get("selector"), str) and entry["selector"]
        ]

    def declared_windows(self):
        """Asked of omp itself rather than read from a file.

        Inheriting `PiHarness`'s file reader instead answered for four models
        where omp knows ninety-seven, and every other one came back with no
        window at all.

        Never fatal, because a run with no window metadata still runs.
        """
        try:
            entries = self._models_json()
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}
        windows = {}
        for entry in entries:
            selector = entry.get("selector")
            context, output = entry.get("contextWindow"), entry.get("maxTokens")
            if selector and isinstance(context, int) and isinstance(output, int):
                windows[selector] = (context, output)
        return windows

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
