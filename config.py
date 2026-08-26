"""Configuration: a global TOML file, overridden per repository.

The predecessor reads one global config and nothing else, which is why its dashboard has to
rewrite ``~/.predecessor/config.yml`` in place just to choose a model for a run.  A
project-local file removes that whole class of hack: per-repo gate commands and
worktree placement live with the repo they describe.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import harness
import models

GLOBAL_CONFIG = Path.home() / ".config" / "lmloop" / "config.toml"
PROJECT_CONFIG = ".lmloop.toml"

DEFAULTS: dict = {
    "agent": {
        # Which agent does the typing.  See harness.py -- the loop needs an
        # argv and a JSON event stream, and nothing else, so swapping one is a
        # config line rather than a rewrite.
        #
        # "pi" also covers anything layered on pi, including the npm package
        # called oh-my-pi, which is an extension pi auto-discovers.  "omp" is a
        # different project of almost the same name -- github.com/can1357/oh-my-pi,
        # a fork with its own binary and its own browser, task and LSP tools --
        # and it is never selected implicitly.
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
        #
        # This list is pi's.  omp rejects `replace` and `ls` outright, so a
        # project that selects omp and leaves this untouched gets omp's own
        # default instead -- see `resolve_tools` below, and docs/operations.md
        # for the allowlist to use when the work has a user interface in it.
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
        # Where omp's browser tool should attach, when the allowlist includes
        # it.  Only omp has a browser; every other harness ignores this.
        #
        # It must be an HTTP CDP *discovery* endpoint -- omp rejects `ws://`
        # and `wss://` by name -- and it must not need a credential in its
        # query string, because omp's attach drops one.  See browser.py, which
        # says which of those you have before a run starts rather than after.
        #
        # Left empty, the browser tool falls back to omp's own configuration:
        # its `browser.cdpUrl` setting, its relay, or a headless Chromium it
        # launches itself.  Setting it here only adds the preflight.
        "browser_cdp_url": "",
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
        # How long to hold when the model server is simply NOT THERE, as opposed
        # to there and failing.  On a workstation whose GPU is also the machine
        # its owner plays games on, llama-swap being stopped for an hour or two
        # is routine, not an incident -- so the loop waits it out and picks the
        # iteration back up rather than burning the run.  Observed before this
        # existed: the server was stopped by hand, the loop retried 1m/2m/4m and
        # then ended the run with "model server unreachable".
        #
        # Deliberately NOT the same policy as a server that answers and
        # misbehaves: that still gets the short 1m/2m/4m backoff, because a
        # server which is up and broken does not fix itself by being waited on.
        # The two are told apart by whether `GET /running` answers at all.
        #
        # 0 restores the old behaviour (give up after the short backoff).
        "server_wait_seconds": 21600,   # 6h
    },
    "worktree": {
        "root": "{repo}/.worktrees/{run_id}",
        "branch": "lmloop/{run_id}",
        # Worktrees are never removed automatically.  The predecessor deletes the worktree
        # of any run that produced no commits, taking the only record of why it
        # produced none with it.
        "keep": "always",
        # Untracked paths to link from the repo into the worktree.
        #
        # `git worktree add` materialises tracked files and nothing else, so a
        # fresh worktree has the source but not the environment that runs it.
        # Watched live on one project: the agent spent an hour and 24 tool calls
        # hunting for a python3 that could import Flask, because flask lives in
        # `~/git/some-project/.venv`, `.venv` is untracked, and the worktree
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
        # "9-10K output tokens, did not finish", and that project has since shown
        # local-fast producing 10184 output tokens in 69 minutes while thrashing on
        # context overflow -- the same signature.  Nobody was counting
        # compactions when local-wide was measured, so a slow model and a model out of
        # room look identical in that number.  ``max_compactions`` below is what
        # tells them apart.
        "timeout_seconds": 14400,
        "stall_seconds": 1200,
        # Give up on an iteration that has overflowed its context this many times
        # without writing anything.  Observed on one project: six overflows in 69
        # minutes, 81 tool calls, all reads.  Each overflow discards everything
        # the agent had read, so the third one is not a slow start, it is a loop.
        # 0 disables the check.
        "max_compactions": 3,
    },
    "planning": {
        "pre_write_file_limit": 3,
        "steps_per_iteration": 1,
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
    "env": {
        # What the agent and the gate see of the host environment.  The default
        # is an allowlist, because the alternative -- and what this was until
        # lm-ka5.9 -- hands every credential in the operator's shell to a
        # process that is about to run arbitrary commands and commit files.
        # See env.py for what the base list covers and why it is broad.
        #
        # "all" restores the old behaviour for anyone who has looked at this and
        # wants their whole environment anyway.
        "inherit": "allowlist",
        # Extra names to pass, exact or with a trailing `*`.  This is also the
        # opt-in for credentials the harness genuinely needs in the environment
        # rather than in its own config file, e.g. "ANTHROPIC_API_KEY": naming
        # one here is an explicit decision and exempts it from the
        # credential-name filter.  That exemption takes the exact name -- a
        # `*` entry still adds variables but never opts a credential in.
        "pass": [],
        # Names to withhold whatever else allowed them.  Wins over everything.
        "block": [],
    },
    "stop": {
        # The point of the project is a big objective worked down over many
        # short iterations, so the iteration cap is not the safety rail -- it
        # was 3, which cannot decompose anything.  `no_diff_iterations` and
        # `max_wall_hours` are the guards that actually stop a run going
        # nowhere, and both watch evidence rather than counting.
        "max_iterations": 20,  # legacy alias; new configs use the two keys below
        "initial_turns": 20,
        "hard_turn_ceiling": 20,
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


def resolve_tools(harness_name: str, tools: str, strict: bool = True) -> str:
    """The tool allowlist, reconciled with the agent that has to accept it.

    Two agents, two vocabularies.  pi takes whatever it is handed and ignores
    what it does not recognise; omp checks the list against its own built-ins
    and exits 1 -- ``Unknown tools in --tools: replace, ls`` -- before it emits
    a single event.  So the default above, which is pi's, is not a default omp
    can be given.

    An allowlist still identical to the shipped one is nobody's decision, so
    selecting omp swaps in omp's.  Anything else is a decision, and gets
    checked rather than replaced: naming a tool the agent does not have fails
    here, where the operator is still reading config, instead of after a run
    has built a worktree, written a prompt and started an iteration.

    Testing the string rather than remembering whether it was written down
    keeps this true wherever the value came from -- the defaults, either config
    file, or `--agent` on the command line, which arrives long after the files
    have been forgotten.
    """
    agent_name = (harness_name or "pi").strip().lower()
    try:
        adapter = harness.get(agent_name)
    except SystemExit:
        if strict:
            raise
        return tools
    if agent_name == "omp" and tools == DEFAULTS["agent"]["tools"]:
        return harness.OMP_DEFAULT_TOOLS
    unknown = adapter.unknown_tools(tools)
    if unknown and strict:
        raise SystemExit(
            f"lmloop: [agent] tools names {', '.join(unknown)}, which {agent_name} "
            f"does not have; it would exit before the first iteration.  Known: "
            f"{', '.join(sorted(adapter.known_tools))}"
        )
    return tools


def load(repo_root: Path) -> dict:
    """Defaults, then global, then project; translate the legacy turn limit."""
    global_config = _read(GLOBAL_CONFIG)
    project_config = _read(repo_root / PROJECT_CONFIG)
    config = _merge(_merge(DEFAULTS, global_config), project_config)
    explicit_stop = {**global_config.get("stop", {}), **project_config.get("stop", {})}
    if "max_iterations" in explicit_stop:
        legacy = explicit_stop["max_iterations"]
        if "initial_turns" not in explicit_stop:
            config["stop"]["initial_turns"] = legacy
        if "hard_turn_ceiling" not in explicit_stop:
            config["stop"]["hard_turn_ceiling"] = legacy
    # Not strict: this is the path `list`, `status` and `prune` take, and a
    # config that would refuse to start a run must still let you read one that
    # is already running.  Starting a run goes through `override_agent`, which
    # is strict, so nothing reaches an agent unchecked.
    config["agent"]["tools"] = resolve_tools(
        config["agent"].get("harness", "pi"), config["agent"].get("tools", ""),
        strict=False,
    )
    return config


def override_agent(config: dict, harness_name: str = "", tools: str = "") -> None:
    """Apply `--agent` / `--tools` from the command line, in place.

    The allowlist has to be settled *after* both, not during either: `--agent
    omp` against a config file that says pi arrives with pi's tool names still
    in hand, and reconciling at load time would have already blessed them.
    """
    if harness_name:
        harness.get(harness_name)  # fail here, not eight lines into a run
        config["agent"]["harness"] = harness_name
    # An agent that is not installed fails the same way an unknown one does, and
    # for the same reason it should fail here: `Popen` raises FileNotFoundError
    # from inside the driver loop, which builds the worktree, writes the prompt,
    # runs the gate probe and *then* dies without a `run:complete`.  omp is
    # installed separately from pi, so a missing binary is a routine input now
    # rather than a broken machine.
    binary = harness.get(config["agent"].get("harness", "pi")).binary
    if not shutil.which(binary):
        raise SystemExit(
            f"lmloop: [agent] harness is {config['agent'].get('harness', 'pi')}, "
            f"whose binary `{binary}` is not on PATH"
        )
    if tools:
        config["agent"]["tools"] = tools
    config["agent"]["tools"] = resolve_tools(
        config["agent"].get("harness", "pi"), config["agent"].get("tools", "")
    )


def sample() -> str:
    """A commented starting config, written by ``lmloop init``."""
    return """\
# lmloop configuration.  Copy to ~/.config/lmloop/config.toml for global
# defaults, or to <repo>/.lmloop.toml to override them for one project.

[agent]
# pi | omp | opencode.  "pi" covers the npm oh-my-pi extension too, because pi
# discovers it and the stream is unchanged.  "omp" is github.com/can1357/oh-my-pi:
# a separate binary with its own browser, task and LSP tools, never selected
# implicitly.  See docs/operations.md for installing it beside pi.
harness = "pi"
model = "llama-swap/local-wide-agent"
# This list is pi's.  omp has no `ls` and rejects names it does not have rather
# than ignoring them, so under `harness = "omp"` either comment this line out --
# which gets omp's own default, "read,write,edit,bash,grep,glob" -- or replace
# it.  For work with a user interface in it, omp's native browser:
#   tools = "read,edit,grep,glob,bash,browser"
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
# Only omp has a browser.  An HTTP CDP discovery endpoint -- not a ws:// URL,
# and not one whose credential rides in the query string; omp's attach drops
# both.  Empty leaves the browser tool to omp's own configuration and skips the
# preflight.
# browser_cdp_url = "http://127.0.0.1:9222"

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

[planning]
pre_write_file_limit = 3  # files allowed before the first write
steps_per_iteration  = 1  # plan steps allowed in one turn

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

[env]
# What the agent -- and the gate -- see of your environment.  The default is an
# allowlist: enough to run a process and a build, plus the harness's own
# namespace (PI_*, OMP_*, OPENCODE_*).  Everything else is left behind,
# including every credential in your shell, for the same reason ".env" is not a
# default under [worktree] link.  A name that looks like a credential is
# dropped even where a prefix rule allowed it, so NODE_* does not bring
# NODE_AUTH_TOKEN with it.  See env.py for the full base list.
inherit = "allowlist"     # "all" restores the pre-allowlist behaviour
# Extra names, exact or with a trailing "*".  Also the opt-in for a credential
# the harness needs in the environment rather than in its own config file --
# that opt-in takes the exact name, so "AWS_*" adds variables but never hands
# over AWS_SECRET_ACCESS_KEY.
pass  = []                # e.g. ["ANTHROPIC_API_KEY"]
block = []                # withheld whatever else allowed them; wins over all

[stop]
# The budget follows the plan: one iteration per step plus retry_allowance
# spare, recomputed as the plan changes.  max_iterations is then the ceiling
# the plan cannot argue past, not the number of steps you expect.
budget_follows_plan = true
retry_allowance     = 5
initial_turns        = 20     # minimum budget before plan-derived growth
hard_turn_ceiling    = 20     # absolute stop; resume --iterations extends it
# max_iterations = 20         # legacy alias for both values above
max_wall_hours       = 10
no_diff_iterations  = 3
"""
