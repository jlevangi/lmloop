import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import preview as preview_module
from preview import Preview


SERVER = """import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'preview-ok')
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
"""


class PreviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.run_dir = self.worktree / ".lmloop" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        self.port = self.free_port()

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def preview(self, **overrides):
        config = {
            "command": [sys.executable, "-u", "-c", SERVER, "{port}"],
            "port": self.port,
            "path": "/",
            "ready_path": "/",
            "url": "",
            "startup_timeout_seconds": 2,
        }
        config.update(overrides)
        return Preview(self.run_dir, {"preview": config})

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.03)
        self.fail("timed out waiting for preview")

    def tearDown(self):
        try:
            self.preview().stop()
        except Exception:
            pass

    def test_real_http_server_reaches_ready_and_stops(self):
        preview = self.preview()
        self.assertEqual("stopped", preview.status()["state"])
        started = preview.start()
        self.assertIn(started["state"], ("starting", "ready"))
        ready = self.wait_for(lambda: preview.status() if preview.status()["state"] == "ready" else None)
        self.assertEqual("ready", ready["state"])
        self.assertEqual(self.port, ready["port"])
        self.assertIn("{host}", ready["url_template"])
        self.assertTrue((self.run_dir / "preview.pid").is_file())
        self.assertTrue((self.run_dir / "preview.state").is_file())
        self.assertTrue((self.run_dir / "preview.log").is_file())
        stopped = preview.stop()
        self.assertEqual("stopped", stopped["state"])
        self.assertFalse(preview._is_live(preview._read_pid()))

    def test_start_is_idempotent_and_command_has_no_shell(self):
        preview = self.preview()
        preview.start()
        again = preview.start()
        self.assertIn(again["state"], ("starting", "ready"))
        meta = json.loads((self.run_dir / "preview.pid").read_text())
        self.assertEqual(str(self.worktree), meta["worktree"])
        self.assertIn(str(self.port), meta["argv"])

    def test_start_waits_for_procfs_identity_before_publishing_pid(self):
        real_start_time = preview_module._proc_start_time
        calls = 0

        def delayed(pid):
            nonlocal calls
            calls += 1
            return None if calls == 1 else real_start_time(pid)

        preview = self.preview()
        with mock.patch.object(preview_module, "_proc_start_time", side_effect=delayed):
            started = preview.start()
        self.assertEqual("starting", started["state"])
        meta = json.loads((self.run_dir / "preview.pid").read_text())
        self.assertTrue(meta["proc_start_time"])
        self.assertTrue(preview._identity(meta))

    def test_output_is_written_directly_to_the_durable_log(self):
        preview = self.preview(command=[
            sys.executable, "-u", "-c",
            "import sys,time; print('durable', flush=True); time.sleep(5)",
        ])
        preview.start()
        self.wait_for(lambda: "durable" in preview.tail())
        self.assertIn("durable", preview.tail())

    def test_stale_pid_and_pid_reuse_are_not_killed(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        def cleanup():
            unrelated.terminate()
            unrelated.wait(timeout=2)
        self.addCleanup(cleanup)
        (self.run_dir / "preview.pid").write_text(json.dumps({
            "pid": unrelated.pid,
            "proc_start_time": "not-the-process",
            "argv": ["not-this-process"],
            "worktree": str(self.worktree),
        }))
        (self.run_dir / "preview.state").write_text(json.dumps({"state": "ready"}))
        state = self.preview().status()
        self.assertEqual("stopped", state["state"])
        self.assertTrue(unrelated.poll() is None)

    def test_disabled_preview_never_starts_a_process(self):
        preview = Preview(self.run_dir, {"preview": {}})
        self.assertEqual("disabled", preview.status()["state"])
        self.assertEqual("disabled", preview.start()["state"])

    def test_archived_run_cannot_be_mutated(self):
        archive = self.root / "archive" / "project" / "run-1"
        archive.mkdir(parents=True)
        archived = Preview(archive, {"preview": {"command": ["false"], "port": 1}})
        self.assertEqual("disabled", archived.status()["state"])
        self.assertEqual("disabled", archived.stop()["state"])


if __name__ == "__main__":
    unittest.main()
