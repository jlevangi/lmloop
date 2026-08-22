import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import config
import prompts
from loop import Run
from rundir import RunDir


class RunPolicyTests(unittest.TestCase):
    def make_run(self, cfg=None):
        root = Path(tempfile.mkdtemp())
        return Run(root, cfg or config.load(root), "objective", run_id="test-run")

    def test_new_stop_keys_define_floor_and_hard_ceiling(self):
        cfg = config._merge(config.DEFAULTS, {"stop": {"initial_turns": 4, "hard_turn_ceiling": 12}})
        run = self.make_run(cfg)
        self.assertEqual(4, run.iteration_floor)
        self.assertEqual(12, run.iteration_ceiling)

    def test_legacy_max_iterations_maps_to_both_limits(self):
        cfg = config._merge(config.DEFAULTS, {"stop": {"max_iterations": 7}})
        cfg["stop"].pop("initial_turns", None)
        cfg["stop"].pop("hard_turn_ceiling", None)
        run = self.make_run(cfg)
        self.assertEqual((7, 7), (run.iteration_floor, run.iteration_ceiling))

    def test_execution_failure_does_not_advance_no_diff_streak(self):
        run = self.make_run()
        run.last_outcome = "agent-error"
        self.assertFalse(run._counts_as_healthy_no_change(None, False))
        run.last_outcome = "ok"
        self.assertTrue(run._counts_as_healthy_no_change(None, False))

    def test_terminal_status_distinguishes_completed_and_ceiling_stop(self):
        root = Path(tempfile.mkdtemp())
        rd = RunDir(root, "run")
        rd.path.mkdir(parents=True)
        rd.write_terminal_status("turn ceiling hit", 9, 8, 10)
        state = json.loads(rd.status_path.read_text())
        self.assertEqual("stopped", state["phase"])
        self.assertEqual("turn ceiling hit", state["stop_reason"])
        rd.write_terminal_status("plan complete (10/10)", 10, 10, 10)
        state = json.loads(rd.status_path.read_text())
        self.assertEqual("completed", state["phase"])

    def test_prompt_granularity_is_configurable(self):
        text = prompts.build(objective="x", number=2, max_iterations=9, branch="b", base="abc", log="", diff="", handoff="", handoff_path="handoff", plan="- [ ] one\n- [ ] two", plan_path="plan", planning={"pre_write_file_limit": 6, "steps_per_iteration": 2})
        self.assertIn("at most six files", text)
        self.assertIn("up to 2 unchecked steps", text)


if __name__ == "__main__":
    unittest.main()
