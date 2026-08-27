"""Picking a run back up, per lm-ka5.2's last uncovered acceptance item.

A run that dies -- a reboot, a closed ssh session, an OOM -- leaves its commits
behind, and starting fresh would build a second worktree and abandon the
handoff chain that makes the next iteration cheap. `Run.attach` re-enters the
same worktree, branch, run directory and handoff instead.

Most of what it does is arithmetic on numbers read off disk, and the two
subtle pieces are the ones with the most to lose: how far the ceiling moves,
and how much of the no-diff streak is carried. Both are pinned here, plus the
whole thing end to end against the fake agent.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import gitops
from loop import Run
from rundir import RunDir

FAKE = Path(__file__).parent / "tools" / "fake-agent"


class AttachTests(unittest.TestCase):
    def make_run(self, state=None, iterations_done=0, objective="do the thing"):
        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        cfg["agent"]["model"] = "fake/model"
        run = Run(root, cfg, objective, run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        (run.rundir.path / "prompt.md").write_text(objective + "\n")
        for number in range(1, iterations_done + 1):
            (run.rundir.path / f"iteration-{number}-prompt.md").write_text("x\n")
        if state is not None:
            run.rundir.run_state_path.write_text(json.dumps(state))
        run.screen = mock.MagicMock()
        # `attach` links the environment and writes git excludes; neither is
        # what these tests are about.
        run.publish_sessions = lambda: None
        run.link_environment = lambda: []
        return run

    def attach(self, run, extra=3):
        with mock.patch.object(gitops, "exclude"):
            return run.attach(extra)

    def test_a_missing_run_directory_is_refused(self):
        root = Path(tempfile.mkdtemp())
        run = Run(root, config.load(root), "obj", run_id="never-existed")
        with self.assertRaises(SystemExit) as caught:
            run.attach(3)
        self.assertIn("no run directory", str(caught.exception))

    def test_a_run_that_still_has_a_loop_is_refused(self):
        """Two loops in one worktree commit over each other and write the same
        status file. A paused run still has a loop."""
        run = self.make_run()
        with mock.patch.object(run.rundir, "holder", return_value=4321):
            with self.assertRaises(SystemExit) as caught:
                run.attach(3)
        message = str(caught.exception)
        self.assertIn("4321", message)
        self.assertIn("paused", message, "say how to tell the two apart")

    def test_it_reports_how_many_iterations_are_already_done(self):
        run = self.make_run(iterations_done=4)
        self.assertEqual(4, self.attach(run))

    def test_a_run_that_never_started_an_iteration_resumes_from_zero(self):
        self.assertEqual(0, self.attach(self.make_run(iterations_done=0)))

    def test_the_objective_comes_from_the_run_not_the_command_line(self):
        """`lmloop resume <id>` is given no objective; the run's own prompt is
        the only place it can come from, and re-deriving it would risk a
        different one."""
        run = self.make_run(objective="the original objective")
        run.objective = ""
        self.attach(run)
        self.assertEqual("the original objective", run.objective)

    # -- the ceiling ------------------------------------------------------

    def test_the_ceiling_extends_past_what_is_already_done(self):
        run = self.make_run(iterations_done=5)
        self.attach(run, extra=3)
        self.assertEqual(8, run.iteration_ceiling)
        self.assertEqual(8, run.max_iterations)

    def test_a_saved_ceiling_higher_than_the_work_done_is_respected(self):
        """The run was allowed twenty and did five; resuming for three more
        means twenty-three, not eight."""
        run = self.make_run(state={"hard_turn_ceiling": 20}, iterations_done=5)
        self.attach(run, extra=3)
        self.assertEqual(23, run.iteration_ceiling)

    def test_the_floor_never_exceeds_the_ceiling(self):
        run = self.make_run(state={"hard_turn_ceiling": 2}, iterations_done=9)
        self.attach(run, extra=1)
        self.assertLessEqual(run.iteration_floor, run.iteration_ceiling)

    # -- the no-diff streak, which is the guard that never lies ------------

    def test_the_streak_survives_a_resume(self):
        """A bare `resume` must not launder the one guard that trusts only
        git."""
        run = self.make_run(state={"no_diff_streak": 2})
        run.config["stop"]["no_diff_iterations"] = 5
        self.attach(run)
        self.assertEqual(2, run.no_diff_streak)

    def test_a_tripped_streak_is_capped_one_below_the_limit(self):
        """A run that stopped ON this guard would otherwise reload a tripped
        streak and exit before iteration 1, having run nothing. A resume buys
        exactly one iteration to prove something changed."""
        run = self.make_run(state={"no_diff_streak": 3})
        run.config["stop"]["no_diff_iterations"] = 3
        self.attach(run)
        self.assertEqual(2, run.no_diff_streak)

    def test_the_cap_leaves_room_for_exactly_one_iteration(self):
        run = self.make_run(state={"no_diff_streak": 99})
        run.config["stop"]["no_diff_iterations"] = 3
        self.attach(run)
        self.assertLess(run.no_diff_streak, run.config["stop"]["no_diff_iterations"])

    def test_a_disabled_guard_carries_the_streak_untouched(self):
        run = self.make_run(state={"no_diff_streak": 7})
        run.config["stop"]["no_diff_iterations"] = 0
        self.attach(run)
        self.assertEqual(7, run.no_diff_streak)

    # -- everything else carried across -----------------------------------

    def test_elapsed_time_is_carried_so_the_wall_clock_is_not_reset(self):
        """Otherwise a run could be resumed indefinitely past its hour limit."""
        run = self.make_run(state={"active_elapsed_seconds": 3600.0})
        self.attach(run)
        self.assertEqual(3600.0, run.elapsed_before)

    def test_thrashed_steps_are_carried(self):
        run = self.make_run(state={"thrashed_steps": {"write the parser": 2}})
        self.attach(run)
        self.assertEqual({"write the parser": 2}, run.thrashed_steps)

    def test_an_empty_run_state_is_not_an_error(self):
        run = self.make_run(state={})
        self.attach(run)
        self.assertEqual(0, run.no_diff_streak)
        self.assertEqual(0.0, run.elapsed_before)

    def test_the_resume_is_recorded_as_such(self):
        run = self.make_run(iterations_done=4)
        self.attach(run)
        starts = [e for e in run.rundir.read_events() if e.get("event") == "run:start"]
        self.assertTrue(starts[-1]["resumed"])
        self.assertEqual(4, starts[-1]["completedIterations"])

    def test_it_claims_the_run(self):
        """So a second resume is refused while this one is going."""
        run = self.make_run()
        self.attach(run)
        self.assertTrue(run.rundir.pid_path.exists())


class ResumeEndToEndTests(unittest.TestCase):
    """The whole lifecycle, twice, against an agent that needs no model.

    One run and one resume, shared by every assertion below: they all ask
    something different about the same pair of invocations, and doing the pair
    per test made this the slowest thing in the suite by an order of magnitude
    for no extra evidence.
    """

    @classmethod
    def setUpClass(cls):
        import os

        cls.work = Path(tempfile.mkdtemp())
        cls.bin = cls.work / "bin"
        cls.bin.mkdir()
        (cls.bin / "pi").symlink_to(FAKE)
        cls.repo = cls.work / "repo"
        cls.repo.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            subprocess.run(["git", *args], cwd=cls.repo, check=True, capture_output=True)
        (cls.repo / "calc.py").write_text("x = 1\n")
        (cls.repo / ".fake-agent.json").write_text(json.dumps({
            "outcome": "ok", "write": "calc.py", "append": "more = 1\n",
            "handoff": "did a bit",
        }))
        (cls.repo / ".lmloop.toml").write_text(
            '[agent]\nharness = "pi"\nmodel = "fake/x"\n\n'
            "[stop]\ninitial_turns = 2\nhard_turn_ceiling = 2\nmax_wall_hours = 1\n\n"
            "[iteration]\ntimeout_seconds = 60\nstall_seconds = 30\n\n"
            "[prune]\nafter_run = false\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=cls.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=cls.repo, check=True,
                       capture_output=True)

        # A scratch HOME, for the same reason tools/smoke needs one: without
        # it, `config.load` inside the subprocess reads the *operator's* real
        # ~/.config/lmloop/config.toml, [notify] block included, and this test
        # pushes a real notification to a real phone on every suite run.
        cls.home = cls.work / "home"
        cls.home.mkdir()
        cls.env = dict(os.environ, PATH=f"{cls.bin}:{os.environ['PATH']}", HOME=str(cls.home))
        cls.first = cls.lmloop("run", "grow calc.py")
        cls.branch_after_first = cls.read_branch()
        cls.commits_after_first = cls.count_commits()
        cls.run_dir = next(next((cls.repo / ".worktrees").iterdir())
                           .joinpath(".lmloop", "runs").iterdir())
        cls.prompts_after_first = sorted(
            p.name for p in cls.run_dir.glob("iteration-*-prompt.md"))
        cls.second = cls.lmloop("resume", "--iterations", "2")

    @classmethod
    def lmloop(cls, *args, timeout=180):
        return subprocess.run(
            [sys.executable, str(Path(__file__).parent / "lmloop.py"), *args],
            cwd=cls.repo, capture_output=True, text=True, timeout=timeout, env=cls.env,
        )

    @classmethod
    def read_branch(cls):
        out = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/lmloop"],
            cwd=cls.repo, capture_output=True, text=True, check=True)
        return out.stdout.split()[0]

    @classmethod
    def count_commits(cls):
        return int(subprocess.run(
            ["git", "rev-list", "--count", f"main..{cls.read_branch()}"],
            cwd=cls.repo, capture_output=True, text=True, check=True).stdout.strip())

    def test_both_invocations_ran_to_a_stop(self):
        self.assertIn("run stopped", self.first.stdout, self.first.stderr[-800:])
        self.assertIn("run stopped", self.second.stdout, self.second.stderr[-800:])

    def test_this_is_not_reading_the_operators_real_config(self):
        """The same claim `tools/smoke` makes, and the same reason: both
        invocations above are real `lmloop.py` subprocesses, and without a
        scratch `HOME` they would read the machine's own
        `~/.config/lmloop/config.toml` -- `[notify]` included -- and push a
        real notification to a real phone on every run of this suite."""
        self.assertFalse((self.home / ".config" / "lmloop").exists(),
                         "a scratch HOME must never gain a real config directory")
        log = self.run_dir / "lmloop.log"
        self.assertNotIn('"event": "notify"', log.read_text())
        self.assertNotIn('"event": "notify:failed"', log.read_text())

    def test_a_resumed_run_continues_the_same_one(self):
        self.assertEqual(self.branch_after_first, self.read_branch(),
                         "same branch, not a second run")
        self.assertGreater(self.count_commits(), self.commits_after_first,
                           "and it added to it")

    def test_iteration_numbering_carries_on_rather_than_restarting(self):
        self.assertIn("resuming after 2 iterations", self.second.stdout)
        self.assertIn("iteration 3", self.second.stdout)
        self.assertNotIn("iteration 1:", self.second.stdout)

    def test_only_one_worktree_is_ever_built(self):
        worktrees = list((self.repo / ".worktrees").iterdir())
        self.assertEqual(1, len(worktrees), worktrees)

    def test_the_run_directory_accumulates_rather_than_being_replaced(self):
        after = sorted(p.name for p in self.run_dir.glob("iteration-*-prompt.md"))
        self.assertEqual(self.prompts_after_first, after[:len(self.prompts_after_first)],
                         "earlier prompts kept")
        self.assertGreater(len(after), len(self.prompts_after_first),
                           "and new ones added")

    def test_the_handoff_chain_is_not_abandoned(self):
        """The reason resume exists rather than starting fresh."""
        self.assertTrue((self.run_dir / "handoff.md").is_file())
        self.assertIn("did a bit", (self.run_dir / "handoff.md").read_text())


if __name__ == "__main__":
    unittest.main()
