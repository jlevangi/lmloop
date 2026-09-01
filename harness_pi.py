"""Adapter for pi and extensions layered on pi."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from harness_base import COMPACTION, MESSAGE_END, TOOL, TOOL_END, Harness, _tail

_PROVIDER = re.compile(r"[A-Za-z0-9][\w.-]*")

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

    def loaded_packages(self):
        """pi's `settings.json` names npm packages it loads at startup.

        A separate mechanism from the extensions directory and just as able to
        gate a tool call -- `@vtstech/pi-security` arrives this way.
        """
        settings = Path(self.config_dir) / "settings.json"
        try:
            loaded = json.loads(settings.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(loaded, dict):
            return []
        return sorted(
            name for name in loaded.get("packages") or []
            if isinstance(name, str) and name
        )

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
                # `target` is deliberately shortened for a phone screen.  It
                # is not enough to identify repetition: two directories can
                # both contain foo.py, and commands can differ after byte 60.
                "identity": json.dumps(args, sort_keys=True, separators=(",", ":")),
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
