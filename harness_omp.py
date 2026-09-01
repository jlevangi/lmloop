"""Adapter for the oh-my-pi (omp) fork."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from harness_base import _tail
from harness_pi import PiHarness

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
