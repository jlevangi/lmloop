"""Finding and reading lmloop runs, for the web UI.

predecessor-dashboard spent 732 lines here, most of it reconstructing the present by parsing
an append-only log to its end -- `parse_activity`, `parse_outcome`,
`parse_managed_log`, and a run-directory cache to make that affordable at all.
None of that is needed against lmloop, which writes `status.json`: the present,
atomically, in one small file, every couple of seconds.  So this module reads
files rather than replaying history, and is a fifth of the size.

What it must still be careful about is honesty.  A crashed run leaves its last
`status.json` behind saying `working`, and a dashboard that believes it shows a
dead run as a live one forever.  Every run therefore gets its state from the age
of that file and from whether the log records a completion -- never from the
`phase` field alone.
"""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

# status.json is rewritten every pi_runner.POLL_SECONDS (2s) while an iteration
# runs.  Two minutes is far outside that, and comfortably inside the gap between
# iterations where the loop is committing rather than polling.
STALE_AFTER_SECONDS = 120

# Enough of a plan or handoff to render without shipping the whole file to a
# phone on every poll.  The detail view fetches the rest.
PREVIEW_CHARS = 2000


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _age_seconds(stamp: str | None) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        written = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - written).total_seconds(), 0.0)


# -- discovery --------------------------------------------------------------


def projects(roots: list[Path]) -> list[dict]:
    """Git repositories one level below each root.

    Grouped by root and de-duplicated by name, earlier roots winning, which is
    the rule predecessor-dashboard's project picker already used and users already expect.
    """
    found: dict[str, dict] = {}
    for root in roots:
        try:
            entries = sorted(path for path in root.iterdir() if path.is_dir())
        except OSError:
            continue
        for path in entries:
            if not (path / ".git").exists():
                continue
            if path.name in found:
                continue
            found[path.name] = {
                "id": path.name,
                "name": path.name,
                "path": str(path),
                "root": str(root),
                "runs": len(run_dirs(path)),
            }
    return list(found.values())


def _worktree_root(project: Path) -> Path:
    """Where this project's run worktrees live, honouring its own config.

    A project may relocate them with `[worktree] root`, and a dashboard that
    assumes the default silently shows no runs for exactly the projects whose
    owner cared enough to configure them.
    """
    template = "{repo}/.worktrees/{run_id}"
    try:
        with (project / ".lmloop.toml").open("rb") as handle:
            configured = (tomllib.load(handle).get("worktree") or {}).get("root")
        if isinstance(configured, str) and configured:
            template = configured
    except (OSError, tomllib.TOMLDecodeError):
        pass
    # A placeholder rather than "": an empty run_id leaves a trailing slash,
    # which Path normalises away, so .parent would climb one level too far.
    return Path(template.format(repo=str(project), run_id="__run__")).parent


def run_dirs(project: Path) -> list[Path]:
    """Every run directory belonging to a project, newest run id first."""
    root = _worktree_root(project)
    try:
        found = [path for path in root.glob("*/.lmloop/runs/*") if path.is_dir()]
    except OSError:
        return []
    return sorted(found, key=lambda path: path.name, reverse=True)


def find(roots: list[Path], project_id: str, run_id: str) -> Path | None:
    for project in projects(roots):
        if project["id"] != project_id:
            continue
        for run_dir in run_dirs(Path(project["path"])):
            if run_dir.name == run_id:
                return run_dir
    return None


# -- reading ----------------------------------------------------------------


def _events(run_dir: Path) -> list[dict]:
    """The run's own event log, parsed.  Small: one line per lifecycle event."""
    parsed = []
    for line in _read_text(run_dir / "lmloop.log").splitlines():
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed


def _state(run_dir: Path, status: dict, events: list[dict]) -> tuple[str, float | None]:
    """What is actually happening, from evidence rather than self-report.

    Order matters.  A finished run keeps its last status file forever, so
    completion is checked before liveness; and a live run that someone has asked
    to stop is still running, so the sentinels are checked after.
    """
    age = _age_seconds(status.get("updated_at"))
    last_lifecycle = ""
    for event in events:
        if event.get("event") in ("run:start", "run:complete"):
            last_lifecycle = event["event"]
    if last_lifecycle == "run:complete":
        return "finished", age
    if age is None:
        return "unknown", None
    if age > STALE_AFTER_SECONDS:
        # The loop stopped writing without recording a completion: killed,
        # OOMed, rebooted.  Its status file still says "working".
        return "stale", age
    if (run_dir / "STOP").exists():
        return "stopping", age
    if (run_dir / "PAUSE").exists():
        return "paused", age
    return "running", age


def _defects(events: list[dict]) -> list[str]:
    """Structural problems the last iteration left behind, if any.

    Worth surfacing on a phone: a run can be committing happily while every file
    it touches has stopped parsing, and that is precisely the state where
    stopping it early is worth something.
    """
    latest: list[str] = []
    for event in events:
        if event.get("event") == "checks:failed":
            latest = event.get("problems", [])
        elif event.get("event") == "iteration:start":
            latest = []
    return latest


def _current_step(text: str) -> str:
    """The first unchecked step: what the run is working on right now.

    This is the single most useful sentence about a live run, and it was
    previously only visible by opening the plan.  Markdown emphasis is stripped
    -- the agent writes `**Fix dark-mode.css conflict**` for itself, and a status
    line should read as a sentence rather than as source.
    """
    import re as _re

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [", "* [")) and stripped[3:4].lower() != "x":
            step = stripped[5:].strip()
            step = _re.sub(r"\*\*([^*]+)\*\*", r"\1", step)
            step = step.replace("`", "")
            return step[:160]
    return ""


def _plan_progress(text: str) -> tuple[int, int]:
    done = total = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [", "* [")):
            total += 1
            if stripped[3:4].lower() == "x":
                done += 1
    return done, total


def summarise(project: dict, run_dir: Path) -> dict:
    """One run, as much as a list view needs and no more."""
    status = _read_json(run_dir / "status.json")
    events = _events(run_dir)
    state, age = _state(run_dir, status, events)
    plan = _read_text(run_dir / "plan.md")
    done, total = _plan_progress(plan)

    commits = 0
    outcomes: list[str] = []
    for event in events:
        if event.get("event") == "iteration:end":
            outcomes.append(event.get("outcome", ""))
        if event.get("event") == "run:complete":
            commits = event.get("commitCount", commits)

    objective = _read_text(run_dir / "prompt.md", 4000).strip()
    return {
        "run_id": run_dir.name,
        "project": project["id"],
        "project_path": project["path"],
        "state": state,
        "age_seconds": round(age) if age is not None else None,
        "objective": objective,
        "title": objective.splitlines()[0][:120] if objective else run_dir.name,
        "model": status.get("model", ""),
        "iteration": status.get("iteration"),
        "max_iterations": status.get("max_iterations"),
        "phase": status.get("phase", ""),
        "last_tool": status.get("last_tool", ""),
        "last_target": status.get("last_target", ""),
        "current_step": _current_step(plan),
        "defects": _defects(events),
        "quiet_seconds": status.get("quiet_seconds"),
        "output_tokens": status.get("output_tokens"),
        "elapsed_seconds": status.get("elapsed_seconds"),
        "tool_calls": status.get("tool_calls"),
        "writes": status.get("writes"),
        "compactions": status.get("compactions"),
        "plan_done": done,
        "plan_total": total,
        "paused": (run_dir / "PAUSE").exists(),
        "stopping": (run_dir / "STOP").exists(),
        "iterations_done": len(outcomes),
        "outcomes": outcomes[-12:],
        "commits": commits,
        "updated_at": status.get("updated_at"),
    }


def detail(project: dict, run_dir: Path) -> dict:
    """Everything the run has written, for the detail view."""
    record = summarise(project, run_dir)
    record.update({
        "plan": _read_text(run_dir / "plan.md"),
        "handoff": _read_text(run_dir / "handoff.md"),
        "notes": _read_text(run_dir / "notes.md"),
        "worktree": str(run_dir.parents[2]),
        "run_dir": str(run_dir),
        "iterations": _iterations(run_dir),
    })
    return record


def _iterations(run_dir: Path) -> list[dict]:
    """Per-iteration outcomes, joined from the event log.

    This is the run's history, and it is the one place worth being precise about
    failure: `thrashing`, `truncated`, `no-action` and `stalled` each mean
    something different about what to change, and collapsing them to "failed"
    throws away the diagnosis the loop worked to produce.
    """
    rows: dict[int, dict] = {}
    for event in _events(run_dir):
        number = event.get("iteration")
        if not isinstance(number, int):
            continue
        if event.get("event") == "iteration:start":
            rows.setdefault(number, {"iteration": number})["started_at"] = event.get("timestamp")
        elif event.get("event") == "iteration:end":
            rows.setdefault(number, {"iteration": number}).update({
                "outcome": event.get("outcome"),
                "seconds": round(event.get("elapsedMs", 0) / 1000),
                "tool_calls": event.get("toolCalls"),
                "writes": event.get("writes"),
                "compactions": event.get("compactions"),
                "commit": event.get("commit"),
                "summary": event.get("summary", ""),
                "gate": event.get("gate", ""),
                "plan_done": event.get("planDone"),
                "plan_total": event.get("planTotal"),
            })
    return [rows[key] for key in sorted(rows)]


def all_runs(roots: list[Path]) -> list[dict]:
    """Every run under every project, most recently updated first."""
    collected = []
    for project in projects(roots):
        for run_dir in run_dirs(Path(project["path"])):
            collected.append(summarise(project, run_dir))
    collected.sort(key=lambda run: (run["updated_at"] or ""), reverse=True)
    return collected
