"""`lmloop doctor`: everything that has to be true before a run can work.

Written because the failures it looks for all used to surface the same way --
several minutes into a run, after a worktree and a branch and a prompt had
been built, in a message about something else.  A missing binary raised
`FileNotFoundError` from inside the driver loop; a model nobody had set failed
at preflight; a gate that cannot execute was discovered by running it.

Each check answers one question, returns rather than prints, and never raises:
a broken environment is exactly when a diagnostic must not add a traceback of
its own.  `check` returns `(name, status, detail)` so the caller decides how to
show them -- see `lmloop.cmd_doctor`.

Ordered roughly by what depends on what, so the first `fail` is usually the
cause and the rest are consequences.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


def _run(argv, timeout=10):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done


def python_check():
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 11):
        return ("python", FAIL, f"{version}; lmloop needs 3.11+ for tomllib")
    return ("python", OK, version)


def git_check(repo: Path | None):
    if not shutil.which("git"):
        return ("git", FAIL, "not on PATH")
    done = _run(["git", "rev-parse", "--show-toplevel"])
    if repo is None or done is None or done.returncode != 0:
        return ("git", WARN, "installed, but this is not a git repository")
    return ("git", OK, f"repository at {done.stdout.strip()}")


def config_check(config_module, repo: Path):
    """Are the config files readable, and free of the mistakes `validate` knows?"""
    found = [path for path in (config_module.GLOBAL_CONFIG,
                               repo / config_module.PROJECT_CONFIG) if path.is_file()]
    problems = []
    for path in found:
        try:
            problems += config_module.validate(config_module._read(path), path)
        except SystemExit as error:            # unreadable or not TOML
            return ("config", FAIL, str(error))
    where = ", ".join(str(p) for p in found) or "none found (using defaults)"
    if problems:
        return ("config", FAIL, f"{len(problems)} problem(s): " + "; ".join(problems))
    return ("config", OK, where)


def harness_check(harness_module, config: dict):
    name = config["agent"].get("harness", "pi")
    try:
        adapter = harness_module.get(name)
    except SystemExit as error:
        return ("harness", FAIL, str(error))
    path = shutil.which(adapter.binary)
    if not path:
        return ("harness", FAIL, f"[agent] harness is {name}, whose binary "
                                 f"`{adapter.binary}` is not on PATH")
    return ("harness", OK, f"{name} at {path}")


def model_check(models_module, config: dict):
    model = config["agent"].get("model", "")
    if not model:
        return ("model", FAIL, "[agent] model is not set; `lmloop models` lists them")
    harness_name = config["agent"].get("harness", "pi")
    window = models_module.declared_window(model, harness_name)
    if window is None:
        # Not fatal: a run works without it, but everything that reasons about
        # room -- the status gauge, thrash escalation -- is blind.
        return ("model", WARN, f"{model}: no declared context window; the "
                               "dashboard gauge and thrash escalation cannot use it")
    return ("model", OK, f"{model}: {window[0]} context + {window[1]} output")


def server_check(models_module, config: dict):
    """Only meaningful when the configured model is served locally."""
    model = config["agent"].get("model", "")
    if not models_module.is_local(model):
        return ("model server", OK, "not a local model; nothing to reach")
    url = config["models"]["llama_swap_url"]
    ok, detail = models_module.preflight(model, url)
    if not ok:
        return ("model server", FAIL, f"{url}: {detail}")
    # `preflight` answers "will this load", which for a name the server has
    # never heard of is an optimistic yes -- it reports the swap it would
    # attempt.  The catalogue is free to ask and says whether the name exists
    # at all, which is the difference between a first run that is slow and one
    # that cannot work.
    name = models_module.local_name(model)
    try:
        known = models_module.available(url)
    except Exception:  # noqa: BLE001 - a diagnostic never raises
        known = []
    if known and name not in known:
        return ("model server", FAIL,
                f"{url}: no model named `{name}`; it serves "
                f"{', '.join(sorted(known)[:4])}"
                + (f" and {len(known) - 4} more" if len(known) > 4 else ""))
    return ("model server", OK, f"{url}: {detail}")


def worktree_check(runrecord_module, repo: Path, config: dict):
    root = runrecord_module.worktree_root(repo, config)
    parent = root if root.is_dir() else root.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not os.access(parent, os.W_OK):
        return ("worktrees", FAIL, f"{root}: {parent} is not writable")
    return ("worktrees", OK, str(root))


def gate_check(config: dict, repo: Path):
    command = config["gate"].get("command", "")
    if not command:
        return ("gate", OK, "none configured")
    program = command.split()[0] if command.split() else ""
    if program and not shutil.which(program) and not (repo / program).exists():
        # The gate runs with cwd = the worktree, so a tracked script is fine
        # and an untracked one will not be there.  Cannot tell which from here.
        return ("gate", WARN, f"`{program}` is not on PATH; it must be a path "
                              "tracked in the repo, or the gate cannot run")
    return ("gate", OK, command)


def _pi_security_verdict(config_dir: Path) -> str:
    """`@vtstech/pi-security` blocks `git`, and defaults to the mode that does.

    Its `max` mode blocks 66 commands, `git` among them, and `getSecurityMode`
    returns `max` when `security.json` does not exist -- so an operator who
    installed it for its critical-command list gets the extended one without
    choosing it.  `basic` keeps all 41 critical blocks and allows `git`.

    That matters here more than it would anywhere else: git is the only witness
    a run has, and an agent that cannot run it cannot show its work.  It is not
    fatal -- lmloop commits with its own `git`, not the agent's -- so this is a
    warning about wasted iterations rather than a refusal.
    """
    try:
        mode = json.loads((config_dir / "security.json").read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        mode = ""
    if mode in ("basic", "off"):
        return ""
    # Name the file. A diagnostic that says "set the mode somewhere" costs the
    # reader the search this check exists to save them.
    return (f"npm:@vtstech/pi-security blocks `git` in max mode, which it uses "
            f"when nothing is set -- run `/security mode basic` in pi, or write "
            f'{{"mode": "basic"}} to {config_dir / "security.json"}')


# Things loaded into an agent that are known to stop a run doing its job, and
# what to do about each.
#
# Not a security opinion, and not a list anybody has to keep current for lmloop
# to work: every entry names one thing lmloop cannot run without and the setting
# that gives it back, and says nothing about whether the extension should be
# installed.  Saying a run will fail is this file's whole purpose; deciding what
# a machine's posture ought to be is not.
#
# One entry, because one has been paid for. A run spent iterations working
# around a `git` it was never going to be allowed to run, and it took two wrong
# investigations to find out why -- both of which would have been one `lmloop
# doctor` away.
BLOCKING_EXTENSIONS = {"npm:@vtstech/pi-security": _pi_security_verdict}


def extensions_check(harness_module, config: dict):
    """Name what is loaded into the agent, because it decides what a run can do.

    What is loaded is invisible from lmloop and not all of it is inert. During
    a real run something here answered for the agent -- `[SECURITY] Blocked
    command: git (max mode) (rule: command_blocklist)` -- and the iteration
    spent its time working around a `git` it was never going to be allowed to
    run, with nothing in the run's own record saying why. It is
    `@vtstech/pi-security`, which blocks `git` in a mode it *defaults* to when
    nobody has chosen one.

    That took two wrong answers to find, and both are the reason this check now
    asks the adapter rather than reading a directory itself. The first blamed
    pi, whose bundle contains none of those strings. The second blamed
    `moshi-hooks.ts`, which was the only third-party file in the extensions
    directory -- and cannot block anything: it spawns a detached notifier whose
    output is discarded. The thing that actually gates tool calls was never in
    that directory at all. It is a package named in pi's `settings.json`, which
    this check could not see.

    An unattended loop cannot answer an approval prompt and cannot argue with a
    denial, so anything able to gate a tool call is worth naming before a run
    rather than after. Reported rather than judged: `model-catalog.js` is
    loaded this way too, and lmloop does not work without it.
    """
    name = config["agent"].get("harness", "pi")
    try:
        adapter = harness_module.get(name)
    except SystemExit:
        return ("agent extensions", OK, "unknown agent")
    if not adapter.config_dir:
        return ("agent extensions", OK, f"lmloop does not know where {name} keeps them")
    loaded = adapter.loaded_extensions()
    if not loaded:
        return ("agent extensions", OK, "none installed")
    # One line, deliberately: `display.out` re-flows whatever it is given to the
    # terminal width, so a newline embedded here becomes a run of spaces rather
    # than a break. Wrapping belongs to the thing that knows the width.
    listing = (f"{len(loaded)} loaded into {name} (each can gate what a run may do): "
               + ", ".join(loaded))
    verdicts = [
        verdict for item in loaded
        if (verdict := BLOCKING_EXTENSIONS.get(item, lambda _: "")(Path(adapter.config_dir)))
    ]
    if verdicts:
        return ("agent extensions", WARN, listing + " -- " + "; ".join(verdicts))
    return ("agent extensions", OK, listing)


def notify_check(config_module, config: dict):
    settings = config.get("notify", {})
    if not settings.get("url"):
        return ("notify", OK, "disabled")
    raw = settings.get("token", "")
    if raw and not config_module.secret(raw):
        return ("notify", WARN, f"{settings['url']}: [notify] token is set to "
                                f"`{raw}` but resolves to nothing")
    return ("notify", OK, settings["url"])


def check(repo: Path, config: dict, modules) -> list[tuple[str, str, str]]:
    """Every check, in dependency order.

    `modules` is passed in rather than imported so this stays testable without
    a repository, a harness or a server -- and so a doctor for the *web* side
    can reuse the pieces that apply to it.
    """
    config_module, harness_module, models_module, runrecord_module = modules
    results = [
        python_check(),
        git_check(repo),
        config_check(config_module, repo),
        harness_check(harness_module, config),
        model_check(models_module, config),
        server_check(models_module, config),
        worktree_check(runrecord_module, repo, config),
        gate_check(config, repo),
        extensions_check(harness_module, config),
        notify_check(config_module, config),
    ]
    return results


def worst(results) -> str:
    """The overall verdict: a fail beats a warn beats ok."""
    statuses = {status for _, status, _ in results}
    if FAIL in statuses:
        return FAIL
    return WARN if WARN in statuses else OK
