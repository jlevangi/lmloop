"""The run's screen: a live status line, and keys to drive it.

A run lasts hours, so the thing you attach to should answer "what is it doing
right now" without you having to read a log. That is the whole job here: a
scrolling history of what has happened, plus one line at the bottom that keeps
moving so an attached terminal never looks dead.

Controls are files first and keystrokes second. A file works from anywhere --
ssh, a phone, `paseo terminal send-keys`, another script -- and survives the
terminal going away, so the keyboard handler does nothing but write the same
files a person could touch by hand:

    touch <run-dir>/PAUSE     hold after the current iteration
    rm    <run-dir>/PAUSE     carry on
    touch <run-dir>/STOP      finish the current iteration, commit, exit

Pausing mid-iteration is deliberately not offered. The model is mid-generation
and there is nothing honest to freeze; the pause takes effect at the iteration
boundary, where the tree is committed and the handoff is written.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time

CLEAR_LINE = "\r\033[2K"


def width() -> int:
    """Terminal width, re-read every time because terminals get resized.

    Falls back to 80 when there is no terminal, which is the case that never
    reaches the status line anyway.
    """
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


def compose(segments: list[tuple[int, str]], columns: int) -> str:
    """Join what fits, dropping the least important segments first.

    This is load-bearing, not decoration. A status line wider than the terminal
    wraps, and once it wraps `\\r` only returns to the start of the last visual
    line -- so the clear leaves the wrapped remnant behind and every refresh
    scrolls a new line. An overlong line does not look slightly wrong, it turns
    the whole display into a spam log. Phone terminals hit this at every width.
    """
    segments = [(priority, text) for priority, text in segments if text]
    if not segments:
        return ""
    keep = [True] * len(segments)
    while True:
        text = "  ".join(text for (_, text), alive in zip(segments, keep) if alive)
        if len(text) <= columns:
            return text
        alive = [i for i, on in enumerate(keep) if on]
        if len(alive) <= 1:
            return text[:columns]
        keep[min(alive, key=lambda i: segments[i][0])] = False


class Screen:
    """Scrolling history plus a sticky status line.

    Falls back to plain sequential prints when stdout is not a terminal, so a
    detached run's log stays readable instead of filling with escape codes.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self._status = ""

    def log(self, text: str = "") -> None:
        """A permanent line. Scrolls; the status line stays below it."""
        if self.tty:
            self.stream.write(CLEAR_LINE + text + "\n")
            self.stream.write(self._status)
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def status(self, segments: list[tuple[int, str]] | str) -> None:
        """The line that keeps moving. Overwritten, never scrolled.

        Always trimmed to one terminal line: see `compose` for why that is a
        correctness requirement rather than tidiness.
        """
        if not self.tty:
            return
        columns = max(width() - 1, 20)
        text = segments if isinstance(segments, str) else compose(segments, columns)
        self._status = text[:columns]
        self.stream.write(CLEAR_LINE + self._status)
        self.stream.flush()

    def close(self) -> None:
        if self.tty:
            self.stream.write(CLEAR_LINE)
            self.stream.flush()
        self._status = ""


def elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


class Keys(threading.Thread):
    """Map keystrokes onto the control files, when there is a keyboard.

    Runs as a daemon and simply exits if stdin is not a terminal -- which is the
    normal case for a detached run, and must not be an error.
    """

    HELP = "keys: [p]ause  [r]esume  [q]uit after this iteration"

    def __init__(self, rundir, screen: Screen):
        super().__init__(daemon=True)
        self.rundir = rundir
        self.screen = screen

    def run(self) -> None:
        try:
            import termios
            import tty
        except ImportError:
            return
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return
        try:
            tty.setcbreak(fd)
            while True:
                key = sys.stdin.read(1)
                if not key:
                    return
                self._handle(key.lower())
        except (OSError, ValueError):
            return
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except (termios.error, ValueError):
                pass

    def _handle(self, key: str) -> None:
        if key == "p":
            self.rundir.pause_path.touch()
            self.screen.log("  paused; will hold after this iteration ([r] to resume)")
        elif key == "r":
            self.rundir.pause_path.unlink(missing_ok=True)
            self.screen.log("  resumed")
        elif key == "q":
            self.rundir.stop_path.touch()
            self.screen.log("  stopping after this iteration; work will be committed")
        elif key == "?":
            self.screen.log("  " + self.HELP)


def wait_while_paused(rundir, screen: Screen, interrupted) -> None:
    """Hold at the iteration boundary for as long as PAUSE exists."""
    if not rundir.paused():
        return
    since = time.monotonic()
    screen.log("  paused")
    while rundir.paused() and not interrupted():
        screen.status([
            (3, f"  paused {elapsed(time.monotonic() - since)}"),
            (1, "[r] or rm PAUSE to resume"),
        ])
        time.sleep(2)
    screen.log(f"  resumed after {elapsed(time.monotonic() - since)}")
