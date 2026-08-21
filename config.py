"""Configuration: a global TOML file, overridden per repository.

the predecessor reads one global config and nothing else, which is why its dashboard has to
rewrite ``~/.predecessor/config.yml`` in place just to choose a model for a run.  A
project-local file removes that whole class of hack: per-repo gate commands and
worktree placement live with the repo they describe.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import models

GLOBAL_CONFIG = Path.home() / ".config" / "lmloop" / "config.toml"
PROJECT_CONFIG = ".lmloop.toml"

DEFAULTS: dict = {
    "agent": {
        # Which agent does the typing.  See harness.py -- the loop needs an
        # argv and a JSON event stream, and nothing else, so swapping one is a
        # config line rather than a rewrite.  "pi" also covers anything layered
        # on pi, such as oh-my-pi, which is an extension pi auto-discovers.
        "harness": "pi",
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
        # Passed to `pi --thinking` when set; empty means pi's default.
        #
        # A reasoning model can deliberate its entire output budget away
        # before it emits a single tool call.  local-fast produced 45k
        # characters weighing up test cases -- "Actually, let me
        # reconsider", twice -- hit the 8192-token cap mid-sentence, and
        # ended the message with the write it was building never sent.
        # local-wide did the same thing at the same cap.  On a local
        # model the deliberation is not free thinking, it is the budget
        # the work needed.
        "thinking": "",
        # Planning and editing are different jobs, and on local hardware they
        # want different models.
        #
        # Deciding what the steps are is a whole-repository question: it wants
        # the widest context available and can afford to be slow, because it
        # happens once per run.  Carrying out a step is a two-file question that
        # happens every iteration, where throughput is what matters and a large
        # window is wasted.  local-wide has a 90112-token prompt budget against
        # local-fast's 49152; local-fast produces real edits at several times the rate.
        #
        # Empty means "use the model above for both", which is the behaviour
        # this had before and remains a perfectly reasonable setting.
        "planner_model": "",
        "planner_thinking": "",
    },
    "models": {
        # llama-swap directly, not through a router.  A router reports model
        # metadata rather than how the weights were loaded, and declaring its
        # numbers killed runs on HTTP 400 mid-iteration.
        #
        # The address itself comes from ~/.config/lmloop/model-budgets.json,
        # which the pi extension reads too -- one place to edit when the box
        # moves, rather than one here and one in a JavaScript file nobody
        # remembers is there.  A repo's own .lmloop.toml still overrides it.
        "llama_swap_url": models.budgets()["llama_swap_url"],
    },
    "worktree": {
        "root": "{repo}/.worktrees/{run_id}",
        "branch": "lmloop/{run_id}",
        # Worktrees are never removed automatically.  the predecessor deletes the worktree
        # of any run that produced no commits, taking the only record of why it
        # produced none with it.
        "keep": "always",
        # Untracked paths to link from the repo into the worktree.
        #
        # `git worktree add` materialises tracked files and nothing else, so a
        # fresh worktree has the source but not the environment that runs it.
        # Watched live on one-project: the agent spent an hour and 24 tool calls
        # hunting for a python3 that could import Flask, because flask lives in
        # `~/git/one-project/.venv`, `.venv` is untracked, and the worktree
        # therefore had no virtualenv at all.  It never wrote a line -- it was
        # stuck trying to verify work it could not run.  The same iteration's
        # gate had already failed `rc=127` for the same reason.
        #
        # Symlinked rather than copied: a virtualenv bakes absolute paths into
        # its shebangs and pyvenv.cfg, so a copy either points back at the
        # original anyway or breaks, and node_modules is too big to duplicate
        # per run.  The trade is that a run shares one environment with the repo
        # and with other runs -- an agent that installs a package changes it for
        # everyone.  That is the right default for a loop whose whole job is to
        # run the project's own code, but it is why this is a list you can empty.
        #
        # Paths that do not exist are skipped, and every name here is added to
        # the git exclude list so `git add -A` cannot sweep the link into a
        # commit.
        "link": [".venv", "venv", "node_modules"],
    },
    "iteration": {
        # local-fast's best measured iteration was 87 minutes; local-wide did
        # not finish one in 100.  Any timeout here is a backstop, not a budget.
        #
        # Treat that local-wide figure as unproven rather than settled.  It rests on
        # "9-10K output tokens, did not finish", and one-project has since shown
        # local-fast producing 10184 output tokens in 69 minutes while thrashing on
        # context overflow -- the same signature.  Nobody was counting
        # compactions when local-wide was measured, so a slow model and a model out of
        # room look identical in that number.  ``max_compactions`` below is what
        # tells them apart.
        "timeout_seconds": 14400,
        "stall_seconds": 1200,
        # Give up on an iteration that has overflowed its context this many times
        # without writing anything.  Observed on one-project: six overflows in 69
        # minutes, 81 tool calls, all reads.  Each overflow discards everything
        # the agent had read, so the third one is not a slow start, it is a loop.
        # 0 disables the check.
        "max_compactions": 3,
    },
    "notify": {
        # A run is unattended for hours by design, so the moment it ends is the
        # moment nobody is watching.  One push when it stops, never per
        # iteration: a notification every twenty minutes for ten hours is a
        # channel you learn to ignore, which costs more than it gives.
        "url": "",            # e.g. "https://ntfy.example.com"
        "topic": "lmloop",
        "token": "",          # bearer, if the server requires one
        # Makes the notification tap through to the run in the dashboard.
        "dashboard_url": "",  # e.g. "https://lmloop.example.com"
    },
    "prune": {
        # Sweep when a run ends, rather than on a timer.  A run is exactly when
        # the disk usage happens and exactly when someone is around to see the
        # result, and a cron job that quietly rewrites run directories at 3am is
        # harder to trust than one line at the end of a run that says what it
        # did.  Nothing is deleted but regenerable bytecode; see prune.py.
        "after_run": True,
        # 0 sweeps every finished run in the repository, including the one that
        # has just ended -- which is the one holding the space.
        "older_than_days": 0.0,
    },
    "gate": {
        "command": "",
        "blocks_commit": False,
    },
    "stop": {
        # The point of the project is a big objective worked down over many
        # short iterations, so the iteration cap is not the safety rail -- it
        # was 3, which cannot decompose anything.  `no_diff_iterations` and
        # `max_wall_hours` are the guards that actually stop a run going
        # nowhere, and both watch evidence rather than counting.
        "max_iterations": 20,
        "max_wall_hours": 10,
        "no_diff_iterations": 3,
        # A fixed iteration count is the wrong shape for a plan whose length is
        # not known when the run starts.  With this on, the budget is recomputed
        # from the plan every iteration -- one per step, plus `retry_allowance`
        # spare -- so a step that needs two attempts does not cost the run its
        # last step, and a plan the agent grows mid-run grows the budget with
        # it.  `max_iterations` stops being the target and becomes the ceiling.
        "budget_follows_plan": True,
        "retry_allowance": 5,
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
harness = "pi"           # pi (incl. oh-my-pi) | opencode
model = "llama-swap/local-wide-agent"
tools = "read,write,edit,bash,grep,find,ls"
# off | minimal | low | medium | high | xhigh | max.  Empty uses pi's
# default.  Lower it when a model deliberates its whole output budget away
# before calling a tool -- both local models here have done exactly that.
thinking = ""
# Writing the plan is a different job from carrying it out: it reads the whole
# repository once per run, so it wants the widest window you have, while editing
# happens every iteration and wants throughput.  Empty uses `model` for both.
planner_model    = ""      # e.g. "llama-swap/local-wide"
planner_thinking = ""

[models]
# Defaults to whatever ~/.config/lmloop/model-budgets.json says, which is also
# what the pi extension reads.  Set this only to point one repo somewhere else.
# llama_swap_url = "http://127.0.0.1:8080"

[worktree]
root   = "{repo}/.worktrees/{run_id}"
branch = "lmloop/{run_id}"
keep   = "always"
# Untracked paths symlinked from the repo into the worktree, so the agent has
# the environment and not just the source.  Missing ones are skipped.  Add
# ".env" here if the project needs it to run -- it is not a default, because it
# would hand the model your secrets without you having asked.
link   = [".venv", "venv", "node_modules"]

[iteration]
timeout_seconds = 14400   # 4h backstop
stall_seconds   = 1200    # 20m of silence from the agent
max_compactions = 3       # give up after N context overflows with no writes

[notify]
url           = ""        # e.g. "https://ntfy.example.com"; empty disables
topic         = "lmloop"
token         = ""        # bearer, if the server requires one
dashboard_url = ""        # so the notification taps through to the run

[prune]
after_run       = true    # compress streams and drop bytecode when a run ends
older_than_days = 0       # 0 = including the run that just finished

[gate]
command       = ""        # e.g. "python -m compileall -q backend"
blocks_commit = false     # record the result; commit either way

[stop]
# The budget follows the plan: one iteration per step plus retry_allowance
# spare, recomputed as the plan changes.  max_iterations is then the ceiling
# the plan cannot argue past, not the number of steps you expect.
budget_follows_plan = true
retry_allowance     = 5
max_iterations      = 20     # the cap, not the plan; git is what stops a bad run
max_wall_hours      = 10
no_diff_iterations  = 3
"""
