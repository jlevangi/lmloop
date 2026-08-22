import json
import tempfile
import time
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
        """Only a turn the agent actually finished counts as going nowhere."""
        run = self.make_run()
        for outcome in ("agent-error", "timeout", "stalled", "thrashing", "interrupted"):
            run.last_outcome = outcome
            self.assertFalse(run._counts_as_no_progress(None, False), outcome)
        # `no-action` is the case the guard exists for: a clean turn, no tool
        # called, nothing written.  Excluding it would leave a model that thinks
        # its whole output budget away running to the ceiling unchallenged.
        for outcome in ("ok", "no-action", "truncated"):
            run.last_outcome = outcome
            self.assertTrue(run._counts_as_no_progress(None, False), outcome)

    def test_uncommitted_work_is_still_progress(self):
        run = self.make_run()
        run.last_outcome = "ok"
        self.assertFalse(run._counts_as_no_progress(None, True))

    def test_wall_clock_counts_each_second_once(self):
        """The segment's own time is measured, never also accumulated.

        Adding the persisted total to `now - started` counted this segment
        twice: a 10h budget stopped a run at six real hours and reported 11.4h.
        """
        cfg = config._merge(config.DEFAULTS, {"stop": {"max_wall_hours": 10}})
        run = self.make_run(cfg)
        run.elapsed_before = 0.0
        six_hours_ago = time.monotonic() - 6 * 3600
        self.assertIsNone(run._abort_reason(2, six_hours_ago))
        eleven_hours_ago = time.monotonic() - 11 * 3600
        self.assertIn("max wall clock", run._abort_reason(2, eleven_hours_ago))

    def test_wall_clock_carries_earlier_segments(self):
        cfg = config._merge(config.DEFAULTS, {"stop": {"max_wall_hours": 10}})
        run = self.make_run(cfg)
        run.elapsed_before = 9 * 3600
        self.assertIsNone(run._abort_reason(2, time.monotonic()))
        run.elapsed_before = 11 * 3600
        self.assertIn("max wall clock", run._abort_reason(2, time.monotonic()))

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
        def render(files, steps):
            return prompts.build(
                objective="x", number=2, max_iterations=9, branch="b", base="abc",
                log="", diff="", handoff="", handoff_path="handoff",
                plan="- [ ] one\n- [ ] two", plan_path="plan",
                planning={"pre_write_file_limit": files, "steps_per_iteration": steps},
            )

        wide = render(6, 2)
        self.assertIn("at most six files", wide)
        self.assertIn("first two unchecked steps", wide)

        # The default keeps the stronger single-step sentence, and the
        # "do not start a second step" clause must never survive into a
        # multi-step prompt that has just asked for several.
        narrow = render(3, 1)
        self.assertIn("at most three files", narrow)
        self.assertIn("FIRST unchecked step and nothing else", narrow)
        self.assertIn("Do not start a second step", narrow)
        self.assertNotIn("Do not start a second step", wide)


if __name__ == "__main__":
    unittest.main()
