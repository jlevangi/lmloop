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
        notify_check(config_module, config),
    ]
    return results


def worst(results) -> str:
    """The overall verdict: a fail beats a warn beats ok."""
    statuses = {status for _, status, _ in results}
    if FAIL in statuses:
        return FAIL
    return WARN if WARN in statuses else OK
