"""Compact prior-run records for iteration prompts."""

import json
from pathlib import Path

def _trim(text: str, limit: int = 88) -> str:
    """Cut at a word boundary; a sentence severed mid-word reads as corruption."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def previous_runs(worktree_root: Path, current_run_id: str, limit: int = 3) -> list[dict]:
    """What earlier runs on this repository attempted, and how they ended.

    A run starts in a fresh worktree and therefore knows nothing about the ones
    before it, so the same repository gets rediscovered from scratch every time
    -- including the dead ends.  The artifacts are already on disk; nothing here
    is new information, it was just never offered to anybody.

    This is deliberately a digest and not the archive.  The full record is 86 MB
    per run, almost all of it raw event stream, and none of that belongs in a
    prompt.  What is worth carrying forward is: what was tried, whether it
    landed, and the one line the agent left about where it got to.
    """
    found = []
    try:
        candidates = sorted(worktree_root.glob("*/.lmloop/runs/*"), reverse=True)
    except OSError:
        return []
    for run_dir in candidates:
        if run_dir.name == current_run_id or not run_dir.is_dir():
            continue
        try:
            objective = (run_dir / "prompt.md").read_text(errors="replace").strip()
        except OSError:
            continue

        commits, iterations, outcomes = 0, 0, []
        try:
            for line in (run_dir / "lmloop.log").read_text(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "iteration:end":
                    iterations += 1
                    outcomes.append(event.get("outcome", ""))
                elif event.get("event") == "run:complete":
                    commits = event.get("commitCount", commits)
        except OSError:
            pass

        handoff = ""
        try:
            lines = (run_dir / "handoff.md").read_text(errors="replace").strip().splitlines()
            handoff = lines[0].strip() if lines else ""
        except OSError:
            pass

        found.append({
            "run_id": run_dir.name,
            "objective": _trim(objective.splitlines()[0]) if objective else run_dir.name,
            "iterations": iterations,
            "commits": commits,
            "outcomes": outcomes,
            "handoff": handoff[:140],
        })
        if len(found) >= limit:
            break
    return found
