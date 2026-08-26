"""Configuration: a global TOML file, overridden per repository.

The predecessor reads one global config and nothing else, which is why its dashboard has to
rewrite ``~/.predecessor/config.yml`` in place just to choose a model for a run.  A
project-local file removes that whole class of hack: per-repo gate commands and
worktree placement live with the repo they describe.

## Why TOML and not YAML

Asked for as `config.yaml`, and answered no, deliberately.  Python's standard
library reads TOML (`tomllib`, 3.11+) and does not read YAML.  The ways out were:
add PyYAML as a dependency, keep TOML, or accept YAML only when PyYAML happens
to be installed.

TOML, because the alternatives cost more than the syntax is worth.  A dependency
would end "standard library only, no build step", which is what lets this be
installed by cloning it and is why a run can start on a machine that has nothing
but Python.  Optional YAML is worse than either: the same file would be read on
one machine and rejected on another, and the failure would arrive as a config
that silently does not exist.  Hand-writing a parser is not on the table -- YAML
has enough edges that a partial one is a liability, and this file would own it
forever.

The gap is small for ten sections of scalars, and TOML's `[section]` maps
exactly onto the shape below.  Worth revisiting if lmloop ever grows a
dependency for another reason; not worth acquiring one for this.

## Compatibility

A config written before a rename keeps working, and the renames so far are
handled in place rather than by a version number:

* `[stop] max_iterations` sets both `initial_turns` and `hard_turn_ceiling`,
  and an explicit new key wins over it.  Translated in `load`, and still a
  valid setting as far as `validate` is concerned -- legacy is not wrong.
* `[models] local_provider` (a string) is read as a one-item `local_providers`;
  see `models.local_providers`.

No `schema_version` here, deliberately, unlike `runrecord.py` -- that one is a
contract between two *programs* that must agree about files on disk, where a
version is what lets a reader refuse politely.  This is a file a person writes,
where a rename is better absorbed than announced.  Add one if a change ever
cannot be: the shim goes in `load`, and `validate` keeps accepting the old
spelling so nobody is told their working config is a mistake.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
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
        # No default, on purpose.  This shipped as `llama-swap/local-fast`, one
        # person's model name on one person's server -- it does not exist even
        # on the machine it was written for any more, so the default could only
        # fail, and it failed *late*: after a worktree, a branch, a prompt and a
        # preflight.  Empty fails immediately, saying what to set.
        "model": "",
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
        # Which model-id prefixes mean "served by that llama-swap".  Seeded from
        # the same shared file, and overridable per repo like everything else
        # here -- a project pointed at a different local server should not have
        # to edit a file the pi extension also reads.  An empty list turns the
        # local path off: no preflight, no measured window, every model's
        # metadata from the agent's own catalogue.
        "local_providers": models.budgets()["local_providers"],
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
    # Only when the operator never chose: an allowlist they typed is theirs,
    # even if this agent will reject half of it -- `unknown_tools` below is
    # where they get told, rather than here where it would be silently
    # replaced.
    if adapter.default_tools and tools == DEFAULTS["agent"]["tools"]:
        return adapter.default_tools
    unknown = adapter.unknown_tools(tools)
    if unknown and strict:
        raise SystemExit(
            f"lmloop: [agent] tools names {', '.join(unknown)}, which {agent_name} "
            f"does not have; it would exit before the first iteration.  Known: "
            f"{', '.join(sorted(adapter.known_tools))}"
        )
    return tools


# What a value of each shape is called when telling somebody they typed the
# wrong one.  `bool` before `int` on purpose: in Python `True` is an `int`, and
# reporting "expects an integer" for a `true` would be nonsense.
_SHAPES = ((bool, "true or false"), (int, "a whole number"), (float, "a number"),
           (str, "a string in quotes"), (list, "a list"))


def _shape(value) -> str:
    for kind, name in _SHAPES:
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _accepts(expected, value) -> bool:
    """Is `value` a usable stand-in for a default of `expected`'s shape?

    One deliberate looseness: a whole number where a float is expected.  TOML
    tells `0` and `0.0` apart and nobody writing `older_than_days = 0` means
    anything different by it.
    """
    if isinstance(expected, bool):
        return isinstance(value, bool)
    if isinstance(expected, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(expected, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(expected))


def _suggest(name: str, known) -> str:
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6)
    return f"; did you mean `{close[0]}`?" if close else ""


def secret(value: str) -> str:
    """Resolve a secret a config points at rather than contains.

    A config file gets copied into a repo, pasted into an issue, and read by the
    agent the loop is driving -- an agent whose whole job is reading files and
    which will happily quote one back.  So a credential belongs somewhere else,
    and this is how a config says where:

        token = "env:NTFY_TOKEN"          from the environment
        token = "file:~/.config/ntfy"     from a file
        token = "!pass show ntfy"         from a command's output
        token = "hunter2"                 literally, still allowed

    The `!command` spelling is omp's, already in use in `~/.omp/agent/models.yml`
    for exactly this; matching it means one convention rather than two.

    A reference that cannot be resolved comes back empty rather than raising: an
    unreachable notification token should cost one line at the end of a run,
    which is what `_announce` already does with a failure, not the run itself.
    Never returns the reference as though it were the value -- sending the
    literal `env:NTFY_TOKEN` as a bearer token would look like an auth failure
    from a server nobody can see.
    """
    if not isinstance(value, str) or not value:
        return ""
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    if value.startswith("file:"):
        try:
            return Path(value[5:].strip()).expanduser().read_text().strip()
        except OSError:
            return ""
    if value.startswith("!"):
        try:
            done = subprocess.run(
                value[1:].strip(), shell=True, capture_output=True,
                text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""
    return value


def require_model(config: dict) -> None:
    """Refuse to start a run with no model, before anything is built.

    Separate from `override_agent`, which settles the *allowlist* and is called
    from places that have no business caring what model is set.  Called where a
    run actually begins, so this lands before a worktree, a branch, a prompt and
    a preflight rather than after them.
    """
    if config["agent"].get("model"):
        return
    agent_name = config["agent"].get("harness", "pi")
    raise SystemExit(
        "lmloop: no model set.  Put one in .lmloop.toml:\n"
        "\n"
        "  [agent]\n"
        '  model = "<provider>/<name>"\n'
        "\n"
        f"  `lmloop models` lists what {agent_name} can reach; "
        "--model overrides it for one run."
    )


def validate(raw: dict, source: Path) -> list[str]:
    """Everything wrong with one config file, as lines somebody can act on.

    A config file is hand-written, and until this existed every mistake in one
    was silent.  A misspelled key is not a smaller version of a wrong value --
    it is *no* value, so the run quietly uses the default and does none of what
    was asked.  Measured on a file with three ordinary slips: `modle` left the
    model at `llama-swap/local-fast`, a `[stopp]` section was discarded whole,
    and `timeout_seconds = "900"` sailed through as a string to be compared
    against a number much later, somewhere that says nothing about config.

    Returns rather than raises, so a caller can decide whether a typo should
    stop a run from starting (it should) or stop you reading one that is
    already going (it should not).
    """
    problems = []
    for section, values in raw.items():
        if section not in DEFAULTS:
            problems.append(
                f"{source}: unknown section `[{section}]`{_suggest(section, DEFAULTS)}"
            )
            continue
        if not isinstance(values, dict):
            problems.append(f"{source}: `{section}` should be a `[{section}]` section")
            continue
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                problems.append(
                    f"{source}: `[{section}] {key}` is not a setting"
                    f"{_suggest(key, DEFAULTS[section])}"
                )
                continue
            expected = DEFAULTS[section][key]
            if not _accepts(expected, value):
                problems.append(
                    f"{source}: `[{section}] {key}` expects {_shape(expected)}, "
                    f"got {_shape(value)} ({value!r})"
                )
    return problems


def load(repo_root: Path, strict: bool = True) -> dict:
    """Defaults, then global, then project; translate the legacy turn limit."""
    global_config = _read(GLOBAL_CONFIG)
    project_config = _read(repo_root / PROJECT_CONFIG)

    problems = (validate(global_config, GLOBAL_CONFIG)
                + validate(project_config, repo_root / PROJECT_CONFIG))
    if problems:
        listed = "\n  ".join(problems)
        if strict:
            raise SystemExit(f"lmloop: config problems:\n  {listed}")
        # Read-only commands still have to work on a config that would refuse
        # to start a run: the whole point of `lmloop status` is the run that is
        # already going, and refusing to show it because of a typo in a setting
        # that run never saw helps nobody.  Said out loud rather than swallowed.
        print(f"lmloop: ignoring config problems:\n  {listed}", file=sys.stderr)

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
# Required; there is no default.  `lmloop models` lists what your agent can
# reach.  Point it directly at a local server rather than through a router --
# a router reports what a model advertises, not how the weights were loaded.
model = "llama-swap/<your-model>"
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
# what the pi extension reads.  Set these only to point one repo somewhere else.
# llama_swap_url = "http://127.0.0.1:8080"
#
# Which model-id prefixes mean "served by that llama-swap", and so get a real
# measured window instead of whatever the agent's catalogue claims.  Point your
# agents DIRECTLY at llama-swap rather than through a router: a router reports
# what a model advertises, not how the weights were loaded, and one reported
# 262144 for a model actually running with --ctx-size 131072.
# local_providers = ["llama-swap"]
#
# An empty list turns the local path off entirely -- no preflight, no measured
# window -- which is the setting for a machine with no local server.
# local_providers = []

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
