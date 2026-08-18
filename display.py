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

import os
import re
import shutil
import sys
import threading
import time

CLEAR_LINE = "\r\033[2K"
RESET = "\033[0m"

# Colour escapes occupy no columns, so every width calculation in this module
# has to measure around them.  Getting this wrong reintroduces the wrapping bug
# `compose` exists to prevent -- the line would look short and wrap anyway.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

# The spinner is the liveness cue: a clock digit ticking over is something you
# have to go looking for, and on a phone, watching a model that emits nothing
# for minutes at a time, "is this still alive" is the only question that matters.
# It advances only while pi is actually emitting, so a frozen spinner means a
# genuinely quiet agent rather than a slow redraw.
_SPINNER_UTF8 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "|/-\\"


def visible(text: str) -> int:
    """Columns `text` occupies, ignoring colour escapes."""
    return len(_SGR.sub("", text))


def clip(text: str, columns: int) -> str:
    """Cut to `columns` visible characters without splitting a colour escape."""
    if visible(text) <= columns:
        return text
    kept: list[str] = []
    used = 0
    index = 0
    while index < len(text) and used < columns:
        match = _SGR.match(text, index)
        if match:
            kept.append(match.group())
            index = match.end()
            continue
        kept.append(text[index])
        used += 1
        index += 1
    return "".join(kept) + (RESET if "\x1b[" in text else "")


class Palette:
    """Colour, or nothing at all.

    Only the eight basic ANSI colours are used.  They inherit the terminal's own
    theme, so the line stays legible on a light phone terminal and a dark desktop
    one alike, which no hard-coded grey or 256-colour ramp manages.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}{RESET}" if self.enabled and text else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("1;31", text)


def colour_enabled(stream) -> bool:
    """Honour NO_COLOR and dumb terminals; otherwise colour when it is a tty."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def width() -> int:
    """Terminal width, re-read every time because terminals get resized.

    Falls back to 80 when there is no terminal, which is the case that never
    reaches the status line anyway.
    """
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


def fit(text: str, columns: int) -> list[str]:
    """Break one log line so the terminal never has to wrap it itself.

    A wrapped *status* line is a correctness bug -- see `compose`.  A wrapped log
    line is only ugly, but on a phone it is ugly constantly: at 40 columns
    "    ok in 1h09m | 81 tool calls | committed abc12345: 3 files changed" is
    three ragged lines with the continuations flush against the left margin, and
    a screen of those reads as noise rather than as a run.  Breaking it here puts
    continuations under a hanging indent, so the shape of the log survives the
    width.

    A word longer than the terminal is never broken and never moved: run ids and
    paths are unbreakable, a hard split through the middle of one cannot be
    copied, and giving it a line of its own only spends a line to overflow
    anyway.  `  repo    /very/long/path` stays one logical line that wraps once,
    rather than becoming a line reading `  repo` and a second that is still too
    wide.  That case is why this is not `textwrap.wrap`.

    Runs of spaces are preserved exactly, because they are what aligns the values
    in a `label    value` header.  Rejoining on single spaces -- which is what
    splitting on whitespace would do -- silently ruins that alignment on every
    line long enough to need attention, which is precisely the lines being fixed.
    """
    if columns < 16 or visible(text) <= columns:
        return [text]
    lead = text[: len(text) - len(text.lstrip(" "))]
    hanging = lead + "  "
    lines: list[str] = []
    current = ""
    for gap, word in re.findall(r"( *)(\S+)", text):
        candidate = current + gap + word
        if not current.strip():
            current = candidate
        elif visible(candidate) <= columns or len(hanging) + visible(word) > columns:
            # Either it fits, or breaking gains nothing because the word
            # overflows a fresh line too.
            current = candidate
        else:
            lines.append(current)
            current = hanging + word
    lines.append(current)
    return lines


def out(text: str = "") -> None:
    """`print`, but pre-broken to the terminal width.

    For the lines a command prints before the run's Screen exists -- the startup
    header, `lmloop status`.  Those are read on a phone more often than anywhere
    else, because they are what you check when you are not at the machine.
    """
    for line in fit(text, max(width() - 1, 20)):
        print(line)


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
        if visible(text) <= columns:
            return text
        alive = [i for i, on in enumerate(keep) if on]
        if len(alive) <= 1:
            return clip(text, columns)
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
        self.paint = Palette(colour_enabled(self.stream))
        # Braille renders in every terminal emulator worth attaching from, but
        # the font behind it is the user's, not ours.  If the stream is not UTF-8
        # there is no argument to have -- fall back to the spinner that has
        # worked on every terminal since the 1980s.
        encoding = (getattr(self.stream, "encoding", "") or "").lower()
        self._frames = _SPINNER_UTF8 if "utf" in encoding else _SPINNER_ASCII
        self._tick = 0

    def spin(self, advance: bool = True) -> str:
        """The next spinner frame, or the current one held still.

        Holding it still is the point: the caller freezes the spinner when the
        agent has gone quiet, so the animation reports the model's liveness
        rather than the loop's own redraw timer.
        """
        if advance:
            self._tick += 1
        return self._frames[self._tick % len(self._frames)]

    def log(self, text: str = "") -> None:
        """A permanent line. Scrolls; the status line stays below it.

        Broken to the terminal width only when there is a terminal: a detached
        run's log is read with `tail` at whatever width the reader has, and
        hard-wrapping it at the writer's width would be guessing.
        """
        if self.tty:
            for line in fit(text, max(width() - 1, 20)):
                self.stream.write(CLEAR_LINE + line + "\n")
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
        self._status = clip(text, columns)
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
