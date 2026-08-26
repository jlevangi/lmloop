"""The run-record contract shared by the runner and the WebUI.

`rundir.py` writes a run's files from inside the worktree that is doing the
work; `web/runs.py` reads the same files from a second process that never
holds a worktree at all -- a run it is showing may be archived, with no
worktree left to hold one. Two readers on one format drifted more than once:

* `RunDir.holder` and `web.runs._holder` implemented the same "is a loop
  still alive here" check twice, one copy keeping a self-pid exclusion the
  other never needed.
* `RunDir.plan_progress`/`.current_step` and `web.runs._plan_progress`/
  `._current_step` each re-implemented the same "scan the plan's checkboxes"
  loop, byte-identical for the counter and subtly different for the step
  text -- the WebUI strips markdown and every backtick for a label meant for
  a human; the runner strips only the pair bracketing the whole step, because
  its answer is fed back into the next prompt close to verbatim.
* `lmloop.py`'s `_status_age` and `web.runs._age_seconds` computed the same
  "how long since this file was touched" from `status.json`'s `updated_at`,
  and each module separately declared the same 120-second staleness cutoff.
* `web/server.py`'s archive/delete/PR handlers guessed a run's branch as
  `f"lmloop/{run_dir.name}"` instead of reading the branch the run actually
  used -- correct only so long as nobody sets `[worktree] branch` to anything
  but its own default. `web.runs.owner()` had the same shape of problem for
  which checkout launched a run: a walk over `.pilot-bases` and worktree-root
  templates, reconstructing an answer the run recorded for itself at
  `run:start` and never needed to guess.
* `lmloop._discover_runs` resolved `[worktree] root` through `config.load`
  (defaults, then `~/.config/lmloop/config.toml`, then the project's own
  `.lmloop.toml`); `web.runs._worktree_root` read the project's `.lmloop.toml`
  with a bare `tomllib.load` and nothing else, so a root relocated only in the
  global config -- exactly the layering `config.load` exists to apply --
  was invisible to the dashboard, which would then report the project as
  having no runs at all.
* `web.runs._events` read `lmloop.log` through the same 200,000-character cap
  used for previewing `plan.md`/`handoff.md`. Fine for those; wrong for an
  append-only lifecycle log, since the cap was anchored to the *start* of the
  file and so hid the *most recent* events on a long, many-iteration run --
  exactly backwards from what a liveness check needs. `RunDir.read_events`
  never had this cap, because the loop needs the whole history; the log this
  reads is small enough (lifecycle events only, not the per-iteration agent
  stream) that neither reader needs one.
* `web.runs._state`/`.summarise` checked `(run_dir / "STOP").exists()` and
  friends directly, duplicating `RunDir.stop_requested`/`.stop_now_requested`/
  `.paused`.

This module is where that reading logic lives once, per lm-ka5.4. Most of
what gets written is still `rundir.py`'s alone; the two exceptions are the
additive `run:start` fields above (`branch` already existed; `repoPath` is
new) and `schema_version` in `status.json` (see below) -- old runs that
predate either fall back to the guesses this module documents. Every function
here takes a bare run directory path -- ``<worktree>/.lmloop/runs/<run-id>``
-- never a `RunDir`, so it works the same whether the caller has a live
worktree or an archived copy with none.

## schema_version

`SCHEMA_VERSION` is what `RunDir.write_status` now stamps into every
`status.json` it writes. It is not consulted by any reader yet -- nothing
here branches on its value -- because nothing has broken compatibility since
it was introduced. It exists so that the day something does, a reader has
somewhere to ask "was this run written before or after that change" instead
of inferring it from which fields happen to be present. `schema_version()`
returns 0, meaning "predates versioning entirely," for a run whose
`status.json` has no such field -- every run before this change, and any run
that fails before its first status write.

## run-state.json and the control sentinels

`run-state.json` holds the resumable half of a run's policy state:
`hard_turn_ceiling`, `no_diff_streak`, `active_elapsed_seconds`, and
`thrashed_steps`, written by `RunDir.write_run_state` and read back by
`Run.attach` on resume (see `loop.py`). `lmloop._read_run_state` is a second,
narrower reader used only by `cmd_resume`, before a `Run` -- and so a
`RunDir` -- exists: it falls back to the last `run:start` event's `agent`/
`tools` fields for a run old enough to predate `run-state.json` itself, which
is not something the resume path's full policy-state read needs, since none
of those numeric fields have a meaningful pre-`run-state.json` fallback
anyway. The two are deliberately not merged into one reader.

The three control sentinels -- `STOP`, `STOP-NOW`, `PAUSE` -- are files whose
only meaningful property is whether they exist; `stop_requested`,
`stop_now_requested`, and `paused` below are that check, named. `STOP` waits
for the in-flight iteration to finish gated, checked, handed off, and
committed before the run exits; `STOP-NOW` cuts it short. A status display
that only needs "is this run winding down" wants `stop_requested`, which is
true for either.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# status.json is rewritten every couple of seconds while an iteration runs.
# Two minutes of silence is far outside that and comfortably inside the gap
# between iterations, where the loop is committing rather than polling.
STALE_AFTER_SECONDS = 120

# Bumped when a change to what gets written would matter to a reader old
# enough to predate it. 0 is not a real version -- it is what `schema_version`
# returns for a run whose `status.json` was written before this field existed
# at all, which includes every run from before this module could stamp one.
SCHEMA_VERSION = 1


def schema_version(run_dir: Path) -> int:
    """The schema version a run's status.json declares, or 0 if unversioned."""
    try:
        value = json.loads((run_dir / "status.json").read_text()).get("schema_version", 0)
    except (OSError, ValueError):
        return 0
    return value if isinstance(value, int) else 0


def holder(run_dir: Path) -> int:
    """The pid of a live lmloop loop on this run, or 0.

    Advisory, and deliberately conservative: it says yes only when the pid is
    running *and* its command line still looks like lmloop, so a recycled pid
    cannot lock a run out. A stale file left by a killed loop reads as 0.

    This says nothing about whose pid it is. A caller that must never mistake
    itself for another loop -- the loop process reading its own run directory
    -- excludes its own pid on top of this; a reader that is structurally never
    the loop, such as the WebUI, has no reason to.
    """
    try:
        pid = int((run_dir / "loop.pid").read_text().strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return 0  # gone, or a platform without /proc: do not block on it
    return pid if is_lmloop_cmdline(cmdline) else 0


# What the loop is actually called, however it was started:
#
#     python3 /home/you/git/lmloop/lmloop.py run "..."     from a clone
#     /home/you/.local/bin/lmloop run "..."                installed
#     /home/you/.local/bin/lmloop-web                      the dashboard
#
# Matched as a program name rather than as a substring.  `b"lmloop" in cmdline`
# said yes to anything that merely *mentioned* it -- a `tail -f` on a path with
# `lmloop` in it, an editor open on a source file, a monitoring shell whose
# script referenced run directories.  That errs safe for archiving, where a
# false holder refuses to remove a worktree, and unsafe for `_state`, where it
# keeps a dead run reading as running instead of stale -- which is the exact
# condition the stale check exists to catch.
#
# Deliberately NOT matched against the run id, which was the first idea: the id
# is derived from the objective *after* launch, so the ordinary
# `lmloop run "objective"` carries no id in its argv at all and would stop
# being recognised as its own run's holder.  Only `resume` and `--detach`'s
# child name one.
_PROGRAM_NAMES = frozenset({b"lmloop", b"lmloop.py", b"lmloop-web"})


def is_lmloop_cmdline(cmdline: bytes) -> bool:
    """Does this `/proc/<pid>/cmdline` belong to an lmloop process?"""
    for argument in cmdline.split(b"\0"):
        if not argument:
            continue
        if argument.rsplit(b"/", 1)[-1] in _PROGRAM_NAMES:
            return True
    return False


def age_seconds(stamp: str | None) -> float | None:
    """Seconds since an ISO timestamp, or None if it is missing or unreadable.

    Used against `status.json["updated_at"]`, which is what every liveness and
    staleness judgement in the runner and the WebUI is actually based on.
    """
    if not isinstance(stamp, str):
        return None
    try:
        written = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - written).total_seconds(), 0.0)


def plan_progress(plan_text: str) -> tuple[int, int]:
    """(done, total) checkbox items in a plan's markdown.

    Counted rather than trusted from prose: the agent reports progress in its
    handoff, and a self-report is not evidence. This is only for a log line or
    a status display -- git remains what decides whether the run got anywhere.
    """
    done = total = 0
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [", "* [")):
            total += 1
            if stripped[3:4].lower() == "x":
                done += 1
    return done, total


def first_unchecked_step(plan_text: str) -> str:
    """Raw text of the first `- [ ]`/`* [ ]` step in a plan, or "".

    Deliberately the *first* one: a plan is ordered, so the earliest unfinished
    step is the one in play, and the runner's prompt and the WebUI's status
    label both have to agree on which step that is or they will name a
    different one to the person reading them.

    Returned raw -- only whitespace-trimmed, nothing stripped from the text
    itself -- because the two callers disagree about what "clean" means:
    `RunDir.current_step` sends this back into the next prompt near verbatim
    and trims only the backtick pair bracketing the whole step; the WebUI's
    `_current_step` is a label for a human and strips markdown emphasis, every
    embedded backtick, and truncates to fit a line. Neither transformation
    belongs here, where both callers would be stuck with whichever it wasn't.
    """
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [", "* [")) and stripped[3:4].lower() != "x":
            return stripped[5:].strip()
    return ""


def latest_run_start(events: list[dict]) -> dict:
    """The most recent `run:start` event in a run's log, or {}.

    A resumed run writes a second `run:start`, and the worktree, branch, and
    owning checkout it names are the ones actually in effect now -- a run
    resumed with a different `--agent`, for instance, still ran in the same
    worktree, but nothing before this event guarantees the *first* start still
    describes the *current* one after a config change or a relocated repo.
    """
    start: dict = {}
    for event in events:
        if event.get("event") == "run:start":
            start = event
    return start


def resolved_branch(run_dir: Path, start: dict) -> str:
    """The branch this run committed to.

    Reads `run:start`'s own `branch` field, written since it was added to
    `loop.py`'s event payload. A run from before that -- or a start event that
    somehow lost the field -- falls back to `lmloop/<run-id>`, which is also
    lmloop's own unconfigured default, so it is right unless the project has
    since set `[worktree] branch` to something else. Callers that reconstruct
    this by hand (`web/server.py`'s delete and PR actions used to) get it
    wrong exactly when that template differs from the default.
    """
    value = start.get("branch")
    return value if isinstance(value, str) and value else f"lmloop/{run_dir.name}"


def resolved_owner(start: dict) -> Path | None:
    """The repository checkout this run's worktree was launched from, if the
    run recorded it, else None.

    Reads `run:start`'s `repoPath`, added alongside `branch`. There is no
    layout-based fallback the way there is for worktree or branch: which
    checkout owns a run is not derivable from the run directory's path at
    all when pilot bases or chained worktrees are involved, which is exactly
    why `web.runs.owner()` used to reconstruct it with a directory walk over
    `.pilot-bases` and every project's own `[worktree] root` template. A
    caller still needs that walk as a fallback for a run from before this
    field existed; this only replaces the guess with the fact when the fact
    is on record.
    """
    value = start.get("repoPath")
    return Path(value) if isinstance(value, str) and value else None


def read_events(run_dir: Path) -> list[dict]:
    """The run's own `lmloop.log`, parsed. One line per lifecycle event --
    small, unlike the raw per-iteration agent stream in `iteration-<n>.jsonl`
    -- so this is read in full rather than capped. Both `RunDir.read_events`
    and `web.runs._events` delegate here; see this module's docstring for why
    a cap was wrong for this particular file.
    """
    try:
        text = (run_dir / "lmloop.log").read_text(errors="replace")
    except OSError:
        return []
    parsed = []
    for line in text.splitlines():
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed


def worktree_root(repo: Path, config: dict) -> Path:
    """Where a repository's run worktrees live, per its own `[worktree] root`.

    `config` is a fully loaded `config.load(repo)` result -- defaults, then
    `~/.config/lmloop/config.toml`, then `repo/.lmloop.toml` -- so a root
    relocated in either config file resolves the same way here as it does for
    the loop that actually builds the worktree. Reading `.lmloop.toml`
    directly, bypassing that layering, is what `web.runs._worktree_root` used
    to do, and it is why a globally relocated root was invisible to it.
    """
    template = config["worktree"]["root"]
    # A placeholder rather than "": an empty run_id leaves a trailing slash,
    # which Path normalises away, so .parent would climb one level too far.
    return Path(template.format(repo=str(repo), run_id="__run__")).parent


def discover_runs(root: Path) -> list[Path]:
    """Every run directory glob-discoverable under one worktree root, sorted.

    This is the single-root half of discovery: the CLI only ever has one
    (`_discover_runs` calls this against `worktree_root(repo, config)`
    directly). The WebUI calls it once per checkout it already knows about --
    a project, each of its `.pilot-bases`, and every worktree in a chained-run
    tree -- because it is the one side that has to find those checkouts in the
    first place; the CLI never does, since it is always invoked with its cwd
    already inside the checkout in question, pilot base or not.
    """
    try:
        return sorted(path for path in root.glob("*/.lmloop/runs/*") if path.is_dir())
    except OSError:
        return []


def stop_requested(run_dir: Path) -> bool:
    """The run should end -- either at the next iteration boundary, or now."""
    return (run_dir / "STOP").exists() or (run_dir / "STOP-NOW").exists()


def stop_now_requested(run_dir: Path) -> bool:
    """The in-flight iteration should be cut short rather than finished."""
    return (run_dir / "STOP-NOW").exists()


def paused(run_dir: Path) -> bool:
    return (run_dir / "PAUSE").exists()
