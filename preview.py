"""Small, durable supervisor for one project's development preview.

Preview commands are intentionally argv-only.  The run directory owns the
metadata, so a web process restart can reconcile a process without guessing
which process belongs to which run.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported dashboard host
    fcntl = None


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


MAX_LOG_BYTES = 1_048_576
LOG_TAIL_BYTES = 12_000
TERM_TIMEOUT_SECONDS = 1.5


def _now() -> float:
    return time.time()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _proc_start_time(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return text.rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _proc_argv(pid: int) -> list[str] | None:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return [item.decode(errors="replace") for item in data.rstrip(b"\0").split(b"\0")]


from urllib.parse import urlparse


_LOCKS: dict[str, threading.RLock] = {}
_CHILDREN: dict[int, subprocess.Popen] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    return _LOCKS.setdefault(key, threading.RLock())


class Preview:
    """Own the preview process associated with one live run worktree."""

    def __init__(self, run_dir: Path, project_config: dict | None = None):
        self.run_dir = Path(run_dir)
        self.config = (project_config or {}).get("preview") or {}
        self.pid_path = self.run_dir / "preview.pid"
        self.state_path = self.run_dir / "preview.state"
        self.log_path = self.run_dir / "preview.log"
        self.worktree = self._worktree()

    def _worktree(self) -> Path | None:
        if self.run_dir.parent.name != "runs" or self.run_dir.parent.parent.name != ".lmloop":
            return None
        return self.run_dir.parents[2]

    def _disabled(self) -> bool:
        return self.worktree is None or not isinstance(self.config, dict) or not self.config.get("command")

    def _read_meta(self) -> dict:
        try:
            value = json.loads(self.pid_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _read_state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _read_pid(self) -> int:
        try:
            return int(self._read_meta().get("pid", 0))
        except (TypeError, ValueError):
            return 0

    def _identity(self, meta: dict) -> bool:
        try:
            pid = int(meta["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if pid <= 0 or _proc_start_time(pid) != str(meta.get("proc_start_time", "")):
            return False
        argv = _proc_argv(pid)
        expected = meta.get("argv")
        if not isinstance(expected, list) or argv != expected:
            return False
        # The persisted worktree is written atomically with this PID's start
        # time and argv. Re-reading /proc/<pid>/cwd is redundant and can fail
        # across a systemd service restart even though those stronger identity
        # fields still match, which orphaned a live preview and made Start report
        # its own port as busy.
        if self.worktree is None:
            return False
        try:
            return Path(str(meta.get("worktree", ""))).resolve() == self.worktree.resolve()
        except (OSError, AttributeError):
            return False

    def _is_live(self, pid: int) -> bool:
        meta = self._read_meta()
        return bool(pid and int(meta.get("pid", 0) or 0) == pid and self._identity(meta))

    def _set_state(self, state: str, **extra) -> dict:
        old = self._read_state()
        result = {**old, "state": state, "updated_at": _now(), **extra}
        if self.worktree is not None:
            _write_json(self.state_path, result)
        return self._public(result)

    def _public(self, state: dict) -> dict:
        meta = self._read_meta()
        result = {
            "enabled": not self._disabled(),
            "state": state.get("state", "stopped"),
            "pid": meta.get("pid"),
            "port": self.config.get("port"),
            "path": self.config.get("path", "/"),
            "url": self._url(),
            "url_template": self._url_template(),
            "started_at": state.get("started_at"),
            "error": state.get("error", ""),
            "log": self.tail(),
            "log_tail": self.tail(),
        }
        return result

    def _url(self) -> str:
        # Keep a configured absolute URL verbatim; templates are resolved by the
        # browser, whose hostname is the trusted public dashboard host.
        value = self.config.get("url", "")
        if isinstance(value, str) and value.strip():
            parsed = urlparse(value)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return value
        return self._url_template()

    def _url_template(self) -> str:
        path = str(self.config.get("path") or "/")
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{{host}}:{self.config.get('port')}{path}"

    def tail(self, limit: int = LOG_TAIL_BYTES) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - min(limit, LOG_TAIL_BYTES)))
                return handle.read().decode(errors="replace")
        except OSError:
            return ""

    def _expanded_argv(self) -> list[str]:
        command = self.config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("preview.command must be a non-empty list of strings")
        port = self.config.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("preview.port must be an integer from 1 to 65535")
        if self.worktree is None:
            raise ValueError("preview run directory is not a live worktree")
        # Replace only the two documented tokens; command arguments may contain
        # ordinary braces (for example Python/JavaScript source).
        return [item.replace("{port}", str(port)).replace("{worktree}", str(self.worktree))
                for item in command]

    @staticmethod
    def _port_busy(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                return True
        except (OSError, ValueError):
            return False

    def _probe_http(self) -> bool:
        port = self.config.get("port")
        if not port:
            return False
        path = str(self.config.get("ready_path") or "/")
        if not path.startswith("/"):
            path = "/" + path
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.1) as sock:
                sock.settimeout(0.1)
                sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
                response = b""
                while b"\r\n\r\n" not in response and len(response) < 8192:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    response += chunk
                lines = response.splitlines()
                if not lines:
                    return False
                first = lines[0]
                return first.startswith(b"HTTP/") and len(first.split()) > 1 and first.split()[1].startswith(b"2")
        except (OSError, ValueError, IndexError):
            return False

    def status(self) -> dict:
        if self._disabled():
            return self._set_state("disabled")
        meta = self._read_meta()
        state = self._read_state()
        if not meta:
            return self._public(state) if state.get("state") == "failed" else self._set_state("stopped", error="")
        if not self._identity(meta):
            # A missing process or a reused PID is not ours. Clear the stale
            # claim and never signal it; the next explicit start gets a clean
            # ownership record. A process that vanished while starting failed.
            self.pid_path.unlink(missing_ok=True)
            if state.get("state") == "starting":
                return self._set_state("failed", error="preview process exited before readiness")
            return self._set_state("stopped", error="")
        current = state.get("state", "starting")
        if current in ("starting", "ready"):
            if self._probe_http():
                return self._set_state("ready", started_at=state.get("started_at"))
            started = float(state.get("started_at") or _now())
            timeout = self.config.get("startup_timeout_seconds", 10)
            if _now() - started >= timeout:
                self._terminate(meta)
                return self._set_state("failed", error="preview readiness timeout")
            return self._set_state("starting", started_at=state.get("started_at"))
        return self._public(state)

    def _bound_log(self) -> None:
        """Keep the persisted log bounded before appending a new process."""
        try:
            size = self.log_path.stat().st_size
            if size <= MAX_LOG_BYTES:
                return
            with self.log_path.open("rb") as source:
                source.seek(-MAX_LOG_BYTES, os.SEEK_END)
                retained = source.read()
            self.log_path.write_bytes(retained)
        except OSError:
            pass

    def start(self) -> dict:
        with _lock_for(self.state_path), _file_lock(self.run_dir / "preview.lock"):
            return self._start_unlocked()

    def _start_unlocked(self) -> dict:
        if self._disabled():
            return self._set_state("disabled")
        existing = self.status()
        if existing["state"] in ("starting", "ready"):
            return existing
        try:
            argv = self._expanded_argv()
        except ValueError as error:
            return self._set_state("failed", error=str(error))
        port = self.config.get("port")
        if port and self._port_busy(int(port)):
            return self._set_state("failed", error=f"port {port} is already in use")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._bound_log()
        try:
            with self.log_path.open("ab", buffering=0) as log:
                child = subprocess.Popen(
                    argv, cwd=self.worktree, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, shell=False, close_fds=True,
                )
        except (OSError, ValueError) as error:
            return self._set_state("failed", error=str(error))
        # Popen may return a few milliseconds before procfs exposes the new
        # process. Never publish an incomplete identity: the first status poll
        # would reject it as PID reuse, clear the claim, and orphan a live server
        # on its configured port.
        deadline = time.monotonic() + 0.5
        proc_start_time = _proc_start_time(child.pid)
        while proc_start_time is None and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
            proc_start_time = _proc_start_time(child.pid)
        if proc_start_time is None:
            child.wait(timeout=0.2)
            return self._set_state("failed", error="preview process exited before identity was recorded")
        meta = {
            "pid": child.pid, "pgid": child.pid, "argv": argv,
            "worktree": str(self.worktree),
            "proc_start_time": proc_start_time,
        }
        _write_json(self.pid_path, meta)
        _CHILDREN[child.pid] = child
        return self._set_state("starting", started_at=_now(), error="")

    def _terminate(self, meta: dict) -> None:
        try:
            pid = int(meta["pid"])
            pgid = int(meta.get("pgid", pid))
        except (KeyError, TypeError, ValueError):
            return
        if not self._identity(meta):
            return
        try:
            if os.getpgid(pid) != pgid:
                return
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        deadline = time.monotonic() + TERM_TIMEOUT_SECONDS
        while time.monotonic() < deadline and self._identity(meta):
            time.sleep(0.03)
        if self._identity(meta):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        # Reap only children launched by this dashboard process. A restarted
        # dashboard has no handle and therefore never waits on an unrelated PID.
        child = _CHILDREN.pop(pid, None)
        if child is not None:
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass

    def stop(self) -> dict:
        with _lock_for(self.state_path), _file_lock(self.run_dir / "preview.lock"):
            return self._stop_unlocked()

    def _stop_unlocked(self) -> dict:
        if self._disabled():
            return self._set_state("disabled")
        meta = self._read_meta()
        if self._identity(meta):
            self._terminate(meta)
        self.pid_path.unlink(missing_ok=True)
        return self._set_state("stopped", error="")

    def restart(self) -> dict:
        with _lock_for(self.state_path), _file_lock(self.run_dir / "preview.lock"):
            if self._disabled():
                return self._set_state("disabled")
            self._stop_unlocked()
            return self._start_unlocked()