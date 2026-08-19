"""Reclaim the space a finished run's event streams take, without losing any.

A run costs about 86 MB, and 81 MB of that is `iteration-*.jsonl`: pi's raw
event stream, which is 88% single-token `message_update` deltas.  Three runs on
one repository came to 313 MB against a 13 MB repository.  Left alone, this
fills a disk.

The obvious fix -- delete old runs -- is the one thing this project will not do.
A run that produced nothing still has to be diagnosable, and every real bug found
in this codebase was found by reading a stream from a run that had already
failed.  So nothing is deleted here either.

Those streams gzip at about 97%, because a file of near-identical JSON objects
is exactly what deflate is for.  Compressing them is a re-encoding, not a loss:
every byte is still there, `rundir` reads either form transparently, and the
files that get read constantly -- plan, handoff, notes, the event log -- are left
uncompressed because they are tiny and being able to `grep` a run directory is
worth more than the kilobytes.
"""

from __future__ import annotations

import gzip
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

# Anything smaller is not worth a syscall, and the CPU costs more than the disk.
MIN_BYTES = 64 * 1024

# A run whose status file is newer than this may still be writing to its stream.
LIVE_WITHIN_SECONDS = 300


def _is_live(run_dir: Path) -> bool:
    """True if this run might still be writing.

    Compressing a file underneath a running iteration would truncate the record
    of whatever it is doing right now, which is the one moment the record cannot
    be reconstructed.
    """
    try:
        state = json.loads((run_dir / "status.json").read_text())
        stamp = state.get("updated_at")
        written = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except (OSError, ValueError):
        return False
    if written is None:
        return False
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - written).total_seconds() < LIVE_WITHIN_SECONDS


def compressible(run_dir: Path) -> list[Path]:
    """The big append-only streams, and only those."""
    found = []
    for pattern in ("iteration-*.jsonl", "sessions/*.jsonl"):
        for path in sorted(run_dir.glob(pattern)):
            if path.is_file() and path.stat().st_size >= MIN_BYTES:
                found.append(path)
    return found


def compress(path: Path) -> tuple[int, int]:
    """Gzip one file in place, returning (before, after).

    Written to a temporary name and renamed, so an interruption leaves either
    the original or the finished archive -- never a half-written one that looks
    complete.
    """
    before = path.stat().st_size
    target = path.with_suffix(path.suffix + ".gz")
    temporary = target.with_suffix(".gz.partial")
    with path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=6) as sink:
        shutil.copyfileobj(source, sink, length=1024 * 1024)
    temporary.replace(target)
    path.unlink()
    return before, target.stat().st_size


def discard_bytecode(run_dir: Path) -> int:
    """Remove the run's bytecode cache, returning the bytes freed.

    `loop.env()` points PYTHONPYCACHEPREFIX here so that compiling a module --
    by the gate, or by anything the agent runs -- does not leave `__pycache__`
    beside the source for `git add -A` to sweep into a commit.  It works, and it
    accumulates: one one-project run had 97 MB of it, more than every event
    stream in that run put together, because the test suite imports the whole
    virtualenv and every module it touches lands here.

    This is the one thing in a run directory that can be deleted without
    argument.  It is derived from source that is still present, it regenerates
    on the next import, and it records nothing whatsoever about what the agent
    did -- which is the actual test the "nothing is discarded" rule applies.
    """
    cache = run_dir / "pycache"
    if not cache.is_dir():
        return 0
    freed = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
    shutil.rmtree(cache, ignore_errors=True)
    return freed


def run_dirs(roots: list[Path]) -> list[Path]:
    found = []
    for root in roots:
        try:
            found += [p for p in root.glob("*/.worktrees/*/.lmloop/runs/*") if p.is_dir()]
            found += [p for p in root.glob(".worktrees/*/.lmloop/runs/*") if p.is_dir()]
        except OSError:
            continue
    return sorted(set(found))


def prune(roots: list[Path], older_than_days: float = 0.0, dry_run: bool = False) -> dict:
    """Compress every finished run's streams. Returns a summary."""
    cutoff = time.time() - older_than_days * 86400
    saved = before_total = after_total = bytecode = 0
    touched: list[str] = []
    skipped_live: list[str] = []

    for run_dir in run_dirs(roots):
        if _is_live(run_dir):
            skipped_live.append(run_dir.name)
            continue

        cache = run_dir / "pycache"
        if cache.is_dir():
            if dry_run:
                bytecode += sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
            else:
                bytecode += discard_bytecode(run_dir)

        for path in compressible(run_dir):
            if older_than_days and path.stat().st_mtime > cutoff:
                continue
            before = path.stat().st_size
            if dry_run:
                before_total += before
                touched.append(str(path))
                continue
            before, after = compress(path)
            before_total += before
            after_total += after
            saved += before - after
            touched.append(str(path))

    return {
        "files": touched,
        "before": before_total,
        "after": after_total,
        "saved": saved,
        "bytecode": bytecode,
        "skipped_live": skipped_live,
    }
