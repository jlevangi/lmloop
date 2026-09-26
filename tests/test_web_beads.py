"""Beads issue picker, bundled skill, operator system prompt."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]

import config  # noqa: E402
import harness  # noqa: E402
import loop  # noqa: E402
import runs as runs_module  # noqa: E402
import service  # noqa: E402


class BeadsListingTests(unittest.TestCase):
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


class LaunchTests(unittest.TestCase):
    def test_issue_is_validated_and_passed_to_the_run(self):
        project = {"id": "p", "path": "/tmp"}
        cfg = {"roots": [], "python": "py", "default_max_iterations": 3,
               "default_model": "", "default_thinking": ""}
        with mock.patch.object(service.runs_module, "projects", return_value=[project]), \
             mock.patch.object(service.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="ok", stderr="")) as run:
            code, _ = service.start_run({"project": "p", "objective": "o", "issue": "x;rm"}, cfg, "l")
            self.assertEqual(code, 400)
            run.assert_not_called()
            code, _ = service.start_run({"project": "p", "objective": "o", "issue": "x-1"}, cfg, "l")
        self.assertEqual(code, 200)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--issue") + 1], "x-1")


class SystemPromptTests(unittest.TestCase):
    def test_pi_and_omp_append_it_opencode_does_not(self):
        common = dict(model="p/m", tools="", thinking="", session_dir="/s", session_id="i",
                      system_prompt="/x.md")
        for name in ("pi", "omp"):
            argv = harness.get(name).argv(**common)
            self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "/x.md")
        self.assertNotIn("--append-system-prompt", harness.get("opencode").argv(**common))
        self.assertNotIn("--append-system-prompt",
                         harness.get("pi").argv(**{**common, "system_prompt": ""}))

    def _run(self, root: Path, issue=""):
        run = SimpleNamespace(repo=root, issue=issue, run_id="r", branch="b",
                              rundir=SimpleNamespace(path=root, event=mock.Mock()))
        run._bd = mock.Mock()
        return run

    def test_assembly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            operator = root / "operator.md"
            with mock.patch.object(config, "SYSTEM_PROMPT", operator):
                run = self._run(root)
                self.assertEqual(loop.Run.system_prompt(run), "")
                operator.write_text("Be terse.")
                (root / ".beads").mkdir()
                run = self._run(root, issue="x-1")
                text = Path(loop.Run.system_prompt(run)).read_text()
        self.assertIn("Be terse.", text)
        self.assertIn("bd create", text)          # the bundled skill
        self.assertIn("bd show x-1", text)

    def test_the_issue_is_no_longer_closed_by_the_loop(self):
        # Closing moved to operator approval: see tests/test_review.py.
        self.assertFalse(hasattr(loop.Run, "_close_issue"))


if __name__ == "__main__":
    unittest.main()
