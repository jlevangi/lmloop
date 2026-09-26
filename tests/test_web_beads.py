"""Beads issue picker: listing and launch wiring."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]

import runs as runs_module  # noqa: E402
import service  # noqa: E402


class BeadsTests(unittest.TestCase):
    def test_no_beads_dir_means_no_issues(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(runs_module.beads_issues(d), [])

    def test_ready_json_is_normalised(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".beads").mkdir()
            out = json.dumps([{"id": "x-1", "title": "T", "priority": 1}])
            done = mock.Mock(returncode=0, stdout=out)
            with mock.patch("shutil.which", return_value="/bin/bd"), \
                 mock.patch("subprocess.run", return_value=done):
                issues = runs_module.beads_issues(d)
        self.assertEqual(issues[0]["id"], "x-1")
        self.assertEqual(issues[0]["issue_type"], "task")

    def test_launch_claims_and_rejects_bad_ids(self):
        project = {"id": "p", "path": "/tmp"}
        cfg = {"roots": [], "python": "py", "default_max_iterations": 3,
               "default_model": "", "default_thinking": ""}
        with mock.patch.object(service.runs_module, "projects", return_value=[project]), \
             mock.patch.object(service.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="ok", stderr="")) as run:
            code, _ = service.start_run({"project": "p", "objective": "o", "issue": "x;rm"}, cfg, "l")
            self.assertEqual(code, 400)
            code, _ = service.start_run({"project": "p", "objective": "o", "issue": "x-1"}, cfg, "l")
        self.assertEqual(code, 200)
        self.assertEqual(run.call_args_list[0].args[0], ["bd", "update", "x-1", "--claim"])
        self.assertIn("bd close x-1", run.call_args_list[1].args[0][3])


if __name__ == "__main__":
    unittest.main()
