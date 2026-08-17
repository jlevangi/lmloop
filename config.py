"""Configuration: a global TOML file, overridden per repository.

the predecessor reads one global config and nothing else, which is why its dashboard has to
rewrite ``~/.predecessor/config.yml`` in place just to choose a model for a run.  A
project-local file removes that whole class of hack: per-repo gate commands and
worktree placement live with the repo they describe.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

GLOBAL_CONFIG = Path.home() / ".config" / "lmloop" / "config.toml"
PROJECT_CONFIG = ".lmloop.toml"

DEFAULTS: dict = {
    "agent": {
        "model": "llama-swap/local-fast",
        # Every extension in ~/.pi/agent/settings.json adds tool definitions,
        # and those escape whatever budget a harness compacts to.  On a 57344
        # declared window this allowlist is the cheapest lever that works.
        #
        # `replace` is pi-hashline-edit-pro's editor.  It is listed because
        # leaving it out does not save an agent from editing -- it just pushes
        # it into `bash` heredocs, which is worse in every way: no diff, no
        # partial-failure reporting, and nothing the event stream can count.
        "tools": "read,write,edit,replace,bash,grep,find,ls",
    },
    "models": {
        # llama-swap directly, not through a router.  A router reports model
        # metadata rather than how the weights were loaded, and declaring its
        # numbers killed runs on HTTP 400 mid-iteration.
        "llama_swap_url": "http://127.0.0.1:8080",
    },
    "worktree": {
        "root": "{repo}/.worktrees/{run_id}",
        "branch": "lmloop/{run_id}",
        # Worktrees are never removed automatically.  the predecessor deletes the worktree
        # of any run that produced no commits, taking the only record of why it
        # produced none with it.
        "keep": "always",
    },
    "iteration": {
        # local-fast's best measured iteration was 87 minutes; local-wide did
        # not finish one in 100.  Any timeout here is a backstop, not a budget.
        "timeout_seconds": 14400,
        "stall_seconds": 1200,
    },
    "gate": {
        "command": "",
        "blocks_commit": False,
    },
    "stop": {
        "max_iterations": 3,
        "max_wall_hours": 10,
        "no_diff_iterations": 3,
    },
}


def _merge(base: dict, override: dict) -> dict:
    """Two-level merge; the config is deliberately only two levels deep."""
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in override.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
        else:
            merged[section] = values
    return merged


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"lmloop: cannot read {path}: {error}") from error


def load(repo_root: Path) -> dict:
    """Defaults, then the global file, then the repo's own ``.lmloop.toml``."""
    config = _merge(DEFAULTS, _read(GLOBAL_CONFIG))
    return _merge(config, _read(repo_root / PROJECT_CONFIG))


def sample() -> str:
    """A commented starting config, written by ``lmloop init``."""
    return """\
# lmloop configuration.  Copy to ~/.config/lmloop/config.toml for global
# defaults, or to <repo>/.lmloop.toml to override them for one project.

[agent]
model = "llama-swap/local-fast"
tools = "read,write,edit,bash,grep,find,ls"

[models]
llama_swap_url = "http://127.0.0.1:8080"

[worktree]
root   = "{repo}/.worktrees/{run_id}"
branch = "lmloop/{run_id}"
keep   = "always"

[iteration]
timeout_seconds = 14400   # 4h backstop
stall_seconds   = 1200    # 20m of silence from the agent

[gate]
command       = ""        # e.g. "python -m compileall -q backend"
blocks_commit = false     # record the result; commit either way

[stop]
max_iterations     = 3
max_wall_hours     = 10
no_diff_iterations = 3
"""
