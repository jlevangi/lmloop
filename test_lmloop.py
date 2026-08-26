import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import os
import signal
import subprocess
import sys
from unittest import mock

import browser
import config
import gitops
import harness
import lmloop
import models
import loop
import pi_runner
import prompts
import runrecord
from loop import Run
from rundir import RunDir


# A process that stays alive *and* carries "lmloop" in its /proc cmdline, for
# the one holder case that asserts a live loop is reported.  It used to be
# `sleep --lmloop-tag=1 300`, which GNU sleep rejects outright as an
# unrecognized option: the child was dead before the assertion ran, and the
# test passed only by beating it to /proc.  A zombie's cmdline reads empty, so
# `holder` saw no "lmloop", returned 0, and the suite failed intermittently.
LMLOOP_LOOKALIKE = [sys.executable, "-c", "import time  # lmloop\ntime.sleep(300)"]


class ResumeStateTests(unittest.TestCase):
    def test_run_start_restores_agent_before_first_completed_iteration(self):
        run_dir = Path(tempfile.mkdtemp())
        (run_dir / "lmloop.log").write_text(json.dumps({
            "event": "run:start", "agent": "omp", "tools": "read,edit,bash",
        }) + "\n")
        self.assertEqual(
            {"harness": "omp", "tools": "read,edit,bash"},
            lmloop._read_run_state(run_dir),
        )


class DiscoverRunsConsolidationTests(unittest.TestCase):
    """`lmloop._discover_runs` now delegates to `runrecord.worktree_root`/
    `.discover_runs`; pins its externally observable (name, path) tuple shape
    and sort order across the delegation."""

    def test_discovers_runs_under_a_configured_worktree_root(self):
        repo = Path(tempfile.mkdtemp())
        cfg = config._merge(config.DEFAULTS, {"worktree": {"root": "{repo}/custom/{run_id}"}})
        for run_id in ("b-run", "a-run"):
            (repo / "custom" / run_id / ".lmloop" / "runs" / run_id).mkdir(parents=True)
        found = lmloop._discover_runs(repo, cfg)
        self.assertEqual(
            [("a-run", repo / "custom" / "a-run" / ".lmloop" / "runs" / "a-run"),
             ("b-run", repo / "custom" / "b-run" / ".lmloop" / "runs" / "b-run")],
            found,
        )

    def test_no_worktree_root_at_all_is_an_empty_list_not_an_error(self):
        repo = Path(tempfile.mkdtemp())
        self.assertEqual([], lmloop._discover_runs(repo, config.load(repo)))


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

    def test_retry_after_context_reset_is_a_transport_failure(self):
        """llama-swap's reset cooldown retries the same turn, not a new one."""
        run = self.make_run()
        run.last_outcome = "agent-error"
        run.last_detail = (
            "400: request (131102 tokens) exceeds the available context size "
            "(reset after 24s) retry-after-ms=24000"
        )
        run.last_commit = None
        self.assertEqual(run.last_detail, run._transport_failure())

        # A genuine oversized prompt has no retry marker and needs correction,
        # not an endless transport retry.
        run.last_detail = "request exceeds the available context size"
        self.assertEqual("", run._transport_failure())

        # Work already captured in Git is never repeated, even if the transport
        # failed after the write.
        run.last_detail = "retry-after-ms=30000"
        run.last_commit = "abc123"
        self.assertEqual("", run._transport_failure())

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

    def test_resume_of_a_no_diff_stop_gets_one_iteration(self):
        """A run stopped on the no-diff guard must still be resumable.

        Reloading a tripped streak made `_abort_reason` fire before iteration 1,
        so the run exited having executed nothing at all.
        """
        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        limit = cfg["stop"]["no_diff_iterations"]
        run = Run(root, cfg, "o", run_id="t")
        run.max_iterations = 50

        run.no_diff_streak = limit          # what a tripped run persists
        self.assertIsNotNone(run._abort_reason(1, time.monotonic()))

        run.rundir.write_run_state({"no_diff_streak": limit})
        carried = min(limit, max(limit - 1, 0))
        run.no_diff_streak = carried        # what attach() now loads
        self.assertIsNone(run._abort_reason(1, time.monotonic()))
        run.no_diff_streak = carried + 1    # one more fruitless iteration
        self.assertIsNotNone(run._abort_reason(2, time.monotonic()))

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

    def test_terminal_status_removes_live_eta(self):
        root = Path(tempfile.mkdtemp())
        rd = RunDir(root, "run")
        rd.path.mkdir(parents=True)
        rd.write_status({"phase": "working", "eta_seconds": 600,
                         "eta_at": "later", "eta_basis": "plan steps",
                         "eta_samples": 3})
        rd.write_terminal_status("plan complete (2/2)", 2, 2, 2)
        state = json.loads(rd.status_path.read_text())
        self.assertFalse(any(key.startswith("eta_") for key in state))

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
        self.assertIn("repository-wide or pathless grep", narrow)
        self.assertIn("read only the matching region", narrow)
        self.assertIn("After three failed", narrow)
        self.assertIn("Do not build an ad-hoc proxy", narrow)
        self.assertNotIn("Do not start a second step", wide)


class AbortReasonAndBudgetCharacterizationTests(unittest.TestCase):
    """Pins `Run._abort_reason`/`._budget` before lm-ka5.2 extracts their pure
    arithmetic into `policy.py`. `budget_follows_plan` defaults to True (see
    config.py DEFAULTS), so `_budget` is live on every unconfigured run, but
    had no direct test coverage before this.
    """

    def make_run(self, cfg=None):
        root = Path(tempfile.mkdtemp())
        run = Run(root, cfg or config.load(root), "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        return run

    # -- _abort_reason ------------------------------------------------------

    def test_interrupted_takes_precedence_over_a_pending_stop_sentinel(self):
        run = self.make_run()
        run.interrupted = True
        run.rundir.stop_path.write_text("")
        self.assertEqual("interrupted", run._abort_reason(2, time.monotonic()))

    def test_stop_now_sentinel_is_reported_and_wins_over_plain_stop(self):
        run = self.make_run()
        run.rundir.stop_path.write_text("")
        run.rundir.stop_now_path.write_text("")
        self.assertEqual("STOP-NOW sentinel present", run._abort_reason(2, time.monotonic()))

    def test_plain_stop_sentinel_is_reported_alone(self):
        run = self.make_run()
        run.rundir.stop_path.write_text("")
        self.assertEqual("STOP sentinel present", run._abort_reason(2, time.monotonic()))

    def test_plan_complete_is_not_reported_at_iteration_one(self):
        """A run's very first iteration cannot have completed a plan it wrote
        during that same iteration -- checked so the guard cannot fire before
        the agent has done anything."""
        run = self.make_run()
        run.rundir.plan_path.write_text("- [x] only step\n")
        self.assertIsNone(run._abort_reason(1, time.monotonic()))

    def test_plan_complete_is_reported_from_iteration_two_on(self):
        run = self.make_run()
        run.rundir.plan_path.write_text("- [x] one\n- [x] two\n")
        self.assertEqual("plan complete (2/2)", run._abort_reason(2, time.monotonic()))

    def test_max_iterations_reached_below_the_hard_ceiling(self):
        run = self.make_run()
        run.max_iterations = 5
        run.iteration_ceiling = 20
        self.assertEqual(
            "max iterations reached (5)", run._abort_reason(6, time.monotonic()),
        )

    def test_turn_ceiling_hit_when_max_iterations_meets_the_ceiling(self):
        run = self.make_run()
        run.max_iterations = 20
        run.iteration_ceiling = 20
        self.assertEqual("turn ceiling hit", run._abort_reason(21, time.monotonic()))

    def test_no_diff_streak_notes_plan_advance_since_run_start(self):
        run = self.make_run()
        limit = run.config["stop"]["no_diff_iterations"]
        run.rundir.plan_path.write_text("- [x] one\n- [ ] two\n")
        run._plan_at_start = 0
        run.no_diff_streak = limit
        reason = run._abort_reason(3, time.monotonic())
        self.assertIn("no git-visible change", reason)
        self.assertIn("plan advanced 0/2 -> 1/2", reason)

    def test_no_diff_streak_notes_no_advance_when_plan_is_unmoved(self):
        run = self.make_run()
        limit = run.config["stop"]["no_diff_iterations"]
        run.rundir.plan_path.write_text("- [x] one\n- [ ] two\n")
        run._plan_at_start = 1
        run.no_diff_streak = limit
        reason = run._abort_reason(3, time.monotonic())
        self.assertIn("plan still at 1/2", reason)

    def test_no_diff_streak_with_no_plan_at_all_has_no_plan_context(self):
        run = self.make_run()
        limit = run.config["stop"]["no_diff_iterations"]
        run.no_diff_streak = limit
        reason = run._abort_reason(3, time.monotonic())
        self.assertIn("no git-visible change", reason)
        self.assertNotIn("plan", reason.split("(")[0])  # no parenthetical at all

    # -- _budget --------------------------------------------------------

    def test_budget_ignores_the_plan_when_not_configured_to_follow_it(self):
        run = self.make_run(config._merge(
            config.DEFAULTS, {"stop": {"budget_follows_plan": False, "initial_turns": 6}},
        ))
        run.rundir.plan_path.write_text("- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
        self.assertEqual(run.iteration_floor, run._budget(50))

    def test_budget_with_no_plan_yet_extends_one_past_the_current_iteration(self):
        run = self.make_run(config._merge(
            config.DEFAULTS, {"stop": {"budget_follows_plan": True, "initial_turns": 3}},
        ))
        self.assertEqual(4, run._budget(3))

    def test_budget_follows_plan_spends_progress_and_adds_slack(self):
        cfg = config._merge(config.DEFAULTS, {
            "stop": {
                "budget_follows_plan": True, "initial_turns": 1,
                "hard_turn_ceiling": 999, "retry_allowance": 5,
            },
        })
        run = self.make_run(cfg)
        run.rundir.plan_path.write_text(
            "- [x] one\n- [x] two\n- [x] three\n- [ ] four\n- [ ] five\n"
        )
        # spent = iteration - 1 = 6; remaining = total - done = 2; +5 slack = 13
        self.assertEqual(13, run._budget(7))

    def test_budget_never_shrinks_below_the_floor(self):
        cfg = config._merge(config.DEFAULTS, {
            "stop": {"budget_follows_plan": True, "initial_turns": 10, "hard_turn_ceiling": 999},
        })
        run = self.make_run(cfg)
        run.rundir.plan_path.write_text("- [x] one\n")
        self.assertEqual(10, run._budget(2))

    def test_budget_is_capped_at_the_hard_ceiling(self):
        cfg = config._merge(config.DEFAULTS, {
            "stop": {
                "budget_follows_plan": True, "initial_turns": 1,
                "hard_turn_ceiling": 8, "retry_allowance": 5,
            },
        })
        run = self.make_run(cfg)
        run.rundir.plan_path.write_text(
            "- [x] one\n- [x] two\n- [x] three\n- [ ] four\n- [ ] five\n"
        )
        self.assertEqual(8, run._budget(7))


class BackoffCharacterizationTests(unittest.TestCase):
    """Pins `Run._backoff`'s delay schedule and give-up threshold before
    lm-ka5.2 extracts the pure "how long, or give up" decision into
    `policy.backoff_delay`. No prior test touched this at all: `_errors` and
    its 1m/2m/4m/give-up progression were only ever exercised live, against
    a real llama-swap outage.

    `_server_is_up` and `_sleep_interruptibly` are mocked so no real network
    call or sleep happens; `_backoff` only reaches the delay/give-up branch
    at all when the server answers, so `_server_is_up` is held True
    throughout -- a down server takes the separate `_wait_for_server` path,
    not this one.
    """

    def make_run(self):
        root = Path(tempfile.mkdtemp())
        run = Run(root, config.load(root), "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        return run

    def test_delay_doubles_each_consecutive_failure_then_gives_up(self):
        run = self.make_run()
        with mock.patch.object(run, "_server_is_up", return_value=True), \
             mock.patch.object(run, "_sleep_interruptibly", return_value=True) as sleep:
            self.assertTrue(run._backoff(1, "boom"))
            self.assertEqual(60, sleep.call_args.args[0])
            self.assertTrue(run._backoff(1, "boom"))
            self.assertEqual(120, sleep.call_args.args[0])
            self.assertTrue(run._backoff(1, "boom"))
            self.assertEqual(240, sleep.call_args.args[0])
            # The fourth consecutive failure gives up rather than backing off
            # a fourth time.
            self.assertFalse(run._backoff(1, "boom"))

    def test_a_recovered_server_resets_the_error_count(self):
        """`_wait_for_server`'s `self._errors = 0` on recovery means a run
        that survives several separate outages never trips the give-up
        threshold on their combined count."""
        run = self.make_run()
        with mock.patch.object(run, "_server_is_up", return_value=True), \
             mock.patch.object(run, "_sleep_interruptibly", return_value=True) as sleep:
            run._backoff(1, "boom")
            run._backoff(1, "boom")
            run._backoff(1, "boom")
            run._errors = 0  # what a recovered server does mid-run
            self.assertTrue(run._backoff(1, "boom"))
            self.assertEqual(60, sleep.call_args.args[0])

    def test_an_unreachable_server_waits_instead_of_backing_off(self):
        run = self.make_run()
        with mock.patch.object(run, "_server_is_up", return_value=False), \
             mock.patch.object(run, "_wait_for_server", return_value=True) as wait:
            self.assertTrue(run._backoff(3, "server down"))
            wait.assert_called_once_with(3, "server down")


class RunDirCharacterizationTests(unittest.TestCase):
    """Pins the run-record reader contract before it gets a canonical home.

    lm-ka5.4 centralizes `RunDir.holder` and `web.runs._holder` behind one
    reader in `runrecord.py`. These tests are written against the pre-refactor
    implementation and must still pass unchanged afterwards -- that is what
    proves the consolidation did not change behaviour.
    """

    def make_rundir(self) -> RunDir:
        root = Path(tempfile.mkdtemp())
        rd = RunDir(root, "run")
        rd.path.mkdir(parents=True)
        return rd

    def test_holder_with_no_pid_file_is_zero(self):
        rd = self.make_rundir()
        self.assertEqual(0, rd.holder())

    def test_holder_never_reports_its_own_pid(self):
        rd = self.make_rundir()
        rd.pid_path.write_text(f"{os.getpid()}\n")
        self.assertEqual(0, rd.holder())

    def test_holder_ignores_garbage_content(self):
        rd = self.make_rundir()
        rd.pid_path.write_text("not-a-pid\n")
        self.assertEqual(0, rd.holder())

    def test_holder_is_zero_for_a_dead_pid(self):
        rd = self.make_rundir()
        proc = subprocess.Popen(["true"])
        proc.wait()
        rd.pid_path.write_text(f"{proc.pid}\n")
        self.assertEqual(0, rd.holder())

    def test_holder_is_zero_for_a_live_pid_that_is_not_lmloop(self):
        rd = self.make_rundir()
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        try:
            rd.pid_path.write_text(f"{proc.pid}\n")
            self.assertEqual(0, rd.holder())
        finally:
            proc.kill()
            proc.wait()

    def test_holder_reports_a_live_pid_whose_cmdline_contains_lmloop(self):
        rd = self.make_rundir()
        proc = subprocess.Popen(LMLOOP_LOOKALIKE, start_new_session=True)
        try:
            rd.pid_path.write_text(f"{proc.pid}\n")
            self.assertEqual(proc.pid, rd.holder())
        finally:
            proc.kill()
            proc.wait()

    def test_claim_then_release_clears_the_pid_file(self):
        rd = self.make_rundir()
        rd.claim()
        self.assertEqual(f"{os.getpid()}\n", rd.pid_path.read_text())
        rd.release()
        self.assertFalse(rd.pid_path.exists())

    def test_release_does_not_clear_another_process_claim(self):
        rd = self.make_rundir()
        rd.pid_path.write_text("999999999\n")
        rd.release()
        self.assertEqual("999999999\n", rd.pid_path.read_text())

    def test_read_events_skips_unparseable_lines_and_tolerates_a_missing_log(self):
        rd = self.make_rundir()
        self.assertEqual([], rd.read_events())
        rd.log_path.write_text('{"event": "run:start"}\nnot json\n{"event": "run:complete"}\n')
        self.assertEqual(
            ["run:start", "run:complete"],
            [event["event"] for event in rd.read_events()],
        )

    def test_plan_progress_counts_checked_boxes_case_insensitively(self):
        rd = self.make_rundir()
        rd.plan_path.write_text("- [x] one\n- [X] two\n- [ ] three\nnot a step\n")
        self.assertEqual((2, 3), rd.plan_progress())

    def test_current_step_is_the_first_unchecked_line(self):
        rd = self.make_rundir()
        rd.plan_path.write_text("- [x] done\n- [ ] `next one`\n- [ ] later\n")
        self.assertEqual("next one", rd.current_step())

    def test_current_step_keeps_interior_backticks(self):
        """Unlike `web.runs._current_step`, which strips every backtick for a
        human-facing label, RunDir's own reading is fed back to the model
        verbatim and only trims the ones bracketing the whole step."""
        rd = self.make_rundir()
        rd.plan_path.write_text("- [ ] `fix `foo.py` bug`\n")
        self.assertEqual("fix `foo.py` bug", rd.current_step())

    def test_plan_problems_flags_a_step_duplicated_across_check_state(self):
        rd = self.make_rundir()
        rd.plan_path.write_text("- [ ] fix the thing\n- [x] fix the thing\n")
        problems = rd.plan_problems()
        self.assertEqual(1, len(problems))
        self.assertIn("plan.md:2", problems[0])

    def test_control_sentinels_reflect_the_files_on_disk(self):
        rd = self.make_rundir()
        self.assertFalse(rd.stop_requested())
        self.assertFalse(rd.stop_now_requested())
        self.assertFalse(rd.paused())
        rd.pause_path.write_text("")
        self.assertTrue(rd.paused())
        rd.stop_path.write_text("")
        self.assertTrue(rd.stop_requested())
        self.assertFalse(rd.stop_now_requested())
        rd.stop_now_path.write_text("")
        self.assertTrue(rd.stop_now_requested())

    def test_schema_version_defaults_a_missing_status_file_to_zero(self):
        rd = self.make_rundir()
        self.assertEqual(0, runrecord.schema_version(rd.path))

    def test_schema_version_reads_a_stamped_value(self):
        """A run's own status.json settles this -- written directly here,
        standing in for a run whose schema_version predates or postdates
        whatever `runrecord.SCHEMA_VERSION` happens to be when this runs."""
        rd = self.make_rundir()
        rd.status_path.write_text(json.dumps({"phase": "working", "schema_version": 3}))
        self.assertEqual(3, runrecord.schema_version(rd.path))

    def test_write_status_stamps_the_current_schema_version(self):
        """Every write through `RunDir.write_status` -- the one choke point
        every status.json write passes through -- declares which version of
        the contract it was written under, so a future incompatible change
        has something to check against for a run written before it landed."""
        rd = self.make_rundir()
        rd.write_status({"phase": "working"})
        self.assertEqual(runrecord.SCHEMA_VERSION, runrecord.schema_version(rd.path))
        self.assertGreater(runrecord.SCHEMA_VERSION, 0)
        # A caller cannot override it by passing its own value: the writer's
        # version is a fact about the writer, never a per-call decision.
        rd.write_status({"phase": "working", "schema_version": 999})
        self.assertEqual(runrecord.SCHEMA_VERSION, runrecord.schema_version(rd.path))

    def test_status_age_matches_the_canonical_reader(self):
        """lmloop.py's `_status_age` must delegate to `runrecord.age_seconds`."""
        from datetime import datetime, timedelta, timezone

        stamp = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        state = {"updated_at": stamp}
        age = lmloop._status_age(state)
        self.assertAlmostEqual(90, age, delta=2)
        self.assertAlmostEqual(runrecord.age_seconds(stamp), age, delta=0.01)
        self.assertIsNone(lmloop._status_age({}))
        self.assertIsNone(runrecord.age_seconds(None))

    def test_stale_after_seconds_is_a_single_shared_constant(self):
        import web.runs as web_runs

        self.assertEqual(120, runrecord.STALE_AFTER_SECONDS)
        self.assertEqual(runrecord.STALE_AFTER_SECONDS, lmloop.STALE_AFTER_SECONDS)
        self.assertEqual(runrecord.STALE_AFTER_SECONDS, web_runs.STALE_AFTER_SECONDS)


class RunStartMetadataTests(unittest.TestCase):
    """The `run:start` event is the one place a run records where it lives.

    web/server.py's archive, delete, and PR actions used to reconstruct the
    worktree path (`run_dir.parents[2]`) and the branch name
    (`f"lmloop/{run_dir.name}"`) instead of reading what the run itself wrote,
    which is wrong for any project with a configured `[worktree] root` or
    `[worktree] branch` template. These tests pin that `prepare()` and
    `attach()` persist `repoPath`, `worktreePath`, and `branch` so a reader
    never has to guess.
    """

    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        for args in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(args, cwd=root, check=True)
        (root / "README").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
        return root

    def test_prepare_persists_repo_worktree_and_branch(self):
        repo = self.make_repo()
        run = Run(repo, config.load(repo), "objective", run_id="test-run")
        run.prepare()
        start = next(
            event for event in run.rundir.read_events() if event["event"] == "run:start"
        )
        self.assertEqual(str(repo), start["repoPath"])
        self.assertEqual(str(run.worktree), start["worktreePath"])
        self.assertEqual(run.branch, start["branch"])

    def test_attach_persists_repo_worktree_and_branch(self):
        repo = self.make_repo()
        run = Run(repo, config.load(repo), "objective", run_id="test-run")
        run.prepare()
        run.rundir.release()

        resumed = Run(repo, config.load(repo), "", run_id="test-run")
        resumed.attach(1)
        starts = [
            event for event in resumed.rundir.read_events() if event["event"] == "run:start"
        ]
        self.assertEqual(str(repo), starts[-1]["repoPath"])
        self.assertEqual(str(resumed.worktree), starts[-1]["worktreePath"])
        self.assertEqual(resumed.branch, starts[-1]["branch"])


class OmpHarnessTests(unittest.TestCase):
    """The omp adapter, against events captured from `omp -p --mode json`.

    Every literal below is a line copied out of a real run of omp v17.4.0
    against a stub OpenAI-compatible provider -- no llama-swap, no cloud model,
    no hand-written approximation of what the stream might look like.  They are
    inline rather than in a fixtures directory because four lines of JSON are
    cheaper to read here than in a file nobody opens.
    """

    def setUp(self):
        self.omp = harness.get("omp")

    # -- argv -------------------------------------------------------------

    def test_argv_omits_session_id(self):
        """`omp --session-id <uuid>` exits 2: the flag does not exist."""
        argv = self.omp.argv(model="p/m", tools="read", thinking="low",
                             session_dir="/s", session_id="iter-3")
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("iter-3", argv)
        self.assertEqual(["omp", "-p", "--mode", "json", "--session-dir", "/s",
                          "--approval-mode", "yolo", "--model", "p/m",
                          "--tools", "read", "--thinking", "low"], argv)

    def test_argv_states_approval_mode(self):
        """Inherited, `always-ask` would hang every iteration until the stall
        clock killed it -- silently, because nobody is there to be asked."""
        argv = self.omp.argv(model="p/m", tools="", thinking="",
                             session_dir="/s", session_id="")
        self.assertEqual("yolo", argv[argv.index("--approval-mode") + 1])

    def test_pi_argv_is_unchanged(self):
        self.assertEqual(
            ["pi", "--model", "p/m", "--mode", "json", "--session-dir", "/s",
             "--session-id", "iter-3", "--tools", "read"],
            harness.get("pi").argv(model="p/m", tools="read", thinking="",
                                   session_dir="/s", session_id="iter-3"),
        )

    # -- events -----------------------------------------------------------

    def test_tool_call_target_and_path(self):
        event = json.loads(
            '{"type": "tool_execution_start", "toolCallId": "call_stub_1",'
            ' "toolName": "read", "args": {"path": "stub_target.txt"}}'
        )
        note = self.omp.classify(event)
        self.assertEqual(harness.TOOL, note["kind"])
        self.assertEqual("read", note["name"])
        self.assertEqual("stub_target.txt", note["target"])
        self.assertEqual("stub_target.txt", note["path"])

    def test_edit_names_its_file_in_a_section_header(self):
        """omp's editor is a patch language: one `input` string, path inside.

        There is no `path` argument to read, so a `files_touched` that looked
        for one would report an omp run editing nothing at all.
        """
        event = json.loads(
            '{"type": "tool_execution_start", "toolCallId": "c1", "toolName": "edit",'
            ' "args": {"input": "[src/app/main.py#AB12]\\nPUT 1.=1:\\n+goodbye\\n"}}'
        )
        note = self.omp.classify(event)
        self.assertEqual("main.py", note["target"])
        self.assertEqual("src/app/main.py", note["path"])

    def test_edit_without_a_usable_header_claims_no_file(self):
        """A wrong path is worse than none: checks would go looking for it."""
        note = self.omp.classify(
            {"type": "tool_execution_start", "toolName": "edit",
             "args": {"input": "PUT 1.=1:\n+no header here"}}
        )
        self.assertIsNone(note["path"])

    def test_browser_call_shows_the_page(self):
        note = self.omp.classify(
            {"type": "tool_execution_start", "toolName": "browser",
             "args": {"action": "open", "url": "http://127.0.0.1:5173/login"}}
        )
        self.assertEqual("http://127.0.0.1:5173/login", note["target"])

    def test_assistant_message_end_carries_usage_and_stop_reason(self):
        event = json.loads(
            '{"type": "message_end", "message": {"role": "assistant",'
            ' "content": [{"type": "text", "text": "Done: the file is a stub."}],'
            ' "api": "openai-completions", "provider": "stub", "model": "stub-tiny",'
            ' "usage": {"input": 1234, "output": 42, "cacheRead": 0, "cacheWrite": 0,'
            ' "totalTokens": 1276}, "stopReason": "stop", "timestamp": 1787515057544}}'
        )
        note = self.omp.classify(event)
        self.assertEqual(harness.MESSAGE_END, note["kind"])
        self.assertEqual("stop", note["stop_reason"])
        self.assertEqual((1234, 42), (note["input"], note["output"]))

    def test_non_assistant_message_ends_are_ignored(self):
        """omp ends a message for the user turn and for every tool result too.

        Counting those would credit the iteration a message end it never had
        and, worse, reset the stop reason to nothing after a real error.
        """
        for role in ("user", "toolResult"):
            self.assertIsNone(
                self.omp.classify({"type": "message_end", "message": {"role": role}}),
                role,
            )

    # -- compaction -------------------------------------------------------

    def test_compaction_events_are_prefixed(self):
        """`auto_compaction_*`, where pi says `compaction_*`.

        pi's markers are a prefix short of omp's, so neither matches the other:
        an overflowing omp iteration would have looked like one that never
        compacted, and the summary -- the best thing such an iteration produces
        -- would have been dropped in favour of a diff of nothing.
        """
        self.assertEqual(b'"auto_compaction_end"', self.omp.compaction_marker)
        self.assertEqual(b'"compaction_end"', harness.get("pi").compaction_marker)
        self.assertEqual(
            harness.COMPACTION,
            self.omp.classify({"type": "auto_compaction_start",
                               "reason": "overflow", "action": "context-full"})["kind"],
        )
        self.assertIsNone(self.omp.classify({"type": "compaction_start"}))

    def test_an_aborted_compaction_carries_nothing(self):
        summary = {"type": "auto_compaction_end", "action": "context-full",
                   "result": {"summary": "half a thought"}, "aborted": True,
                   "willRetry": True}
        self.assertEqual("", self.omp.compaction_summary(summary))
        summary["aborted"] = False
        self.assertEqual("half a thought", self.omp.compaction_summary(summary))

    def test_an_agent_that_does_not_compact_declares_no_marker(self):
        self.assertEqual(b"", harness.get("opencode").compaction_marker)


class ToolAllowlistTests(unittest.TestCase):
    """omp validates `--tools` and exits 1; pi ignores what it does not know."""

    def test_pis_default_allowlist_is_invalid_for_omp(self):
        self.assertEqual(
            ["replace", "ls"],
            harness.get("omp").unknown_tools(config.DEFAULTS["agent"]["tools"]),
        )
        self.assertEqual([], harness.get("pi").unknown_tools("read,replace,ls"))

    def test_selecting_omp_swaps_in_omps_own_default(self):
        default = config.DEFAULTS["agent"]["tools"]
        self.assertEqual(harness.OMP_DEFAULT_TOOLS, config.resolve_tools("omp", default))
        self.assertEqual(default, config.resolve_tools("pi", default))

    def test_a_deliberate_allowlist_survives_the_swap(self):
        self.assertEqual(
            harness.OMP_UI_TOOLS, config.resolve_tools("omp", harness.OMP_UI_TOOLS)
        )

    def test_an_impossible_allowlist_fails_before_the_run(self):
        with self.assertRaises(SystemExit) as caught:
            config.resolve_tools("omp", "read,replace")
        self.assertIn("replace", str(caught.exception))

    def test_override_settles_the_allowlist_after_the_agent(self):
        """`--agent omp` arrives holding pi's tool names; order decides."""
        cfg = config._merge(config.DEFAULTS, {})
        config.override_agent(cfg, "omp")
        self.assertEqual(harness.OMP_DEFAULT_TOOLS, cfg["agent"]["tools"])
        self.assertEqual("omp", cfg["agent"]["harness"])

    def test_override_rejects_an_agent_that_does_not_exist(self):
        with self.assertRaises(SystemExit):
            config.override_agent(config._merge(config.DEFAULTS, {}), "ompp")

    def test_missing_agent_binary_fails_before_a_worktree_is_built(self):
        cfg = config._merge(config.DEFAULTS, {})
        with mock.patch("config.shutil.which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "binary `omp` is not on PATH"):
                config.override_agent(cfg, "omp")

    def test_read_only_config_load_tolerates_an_unavailable_agent(self):
        root = Path(tempfile.mkdtemp())
        (root / ".lmloop.toml").write_text(
            '[agent]\nharness = "future-agent"\ntools = "read"\n'
        )
        loaded = config.load(root)
        self.assertEqual("future-agent", loaded["agent"]["harness"])


class BrowserPreflightTests(unittest.TestCase):
    """A CDP endpoint is credentials; the preflight must never widen them."""

    def test_a_websocket_endpoint_is_not_attachable(self):
        ok, detail = browser.preflight("wss://browser.example/devtools/browser/abc?token=s3cret")
        self.assertFalse(ok)
        self.assertIn("websocket", detail)
        self.assertNotIn("s3cret", detail)

    def test_nothing_configured_is_said_plainly(self):
        ok, detail = browser.preflight("")
        self.assertFalse(ok)
        self.assertIn("no browser CDP endpoint", detail)

    def test_redaction_keeps_the_shape_and_drops_every_value(self):
        redacted = browser.redact("http://127.0.0.1:9222/x?token=s3cret&other=alsosecret")
        self.assertNotIn("s3cret", redacted)
        self.assertNotIn("alsosecret", redacted)
        self.assertIn("127.0.0.1:9222", redacted)
        self.assertIn("token=", redacted)

    def test_redaction_drops_userinfo_as_well_as_query_values(self):
        redacted = browser.redact(
            "http://alice:password@127.0.0.1:9222/x?token=s3cret"
        )
        for secret in ("alice", "password", "s3cret"):
            self.assertNotIn(secret, redacted)
        self.assertIn("<redacted>@127.0.0.1:9222", redacted)

    def test_malformed_endpoint_is_optional_not_an_exception(self):
        ok, detail = browser.preflight("http://127.0.0.1:not-a-port")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_an_unreachable_endpoint_is_not_fatal_and_is_redacted(self):
        # Port 9 is discard: nothing listens, and the failure is immediate.
        ok, detail = browser.preflight("http://127.0.0.1:9/?token=s3cret", timeout=2)
        self.assertFalse(ok)
        self.assertNotIn("s3cret", detail)


class TerminationTests(unittest.TestCase):
    """Killing an iteration has to take the agent's children with it.

    pi and omp both spawn shells, and both reap only the children they tracked.
    `_terminate` signals the whole process group instead, which is why the
    runner starts every agent in a session of its own.  Verified here against a
    real process tree rather than an agent, so it stays honest without a model:
    the mechanism is the same one either agent gets.
    """

    def test_terminate_kills_the_whole_group(self):
        parent = subprocess.Popen(
            ["sh", "-c", "sleep 300 & echo $! ; wait"],
            stdout=subprocess.PIPE, start_new_session=True,
        )
        try:
            child = int(parent.stdout.readline())
            os.kill(child, 0)  # alive, and not our child -- only the group links us
            pi_runner._terminate(parent)
            self.assertIsNotNone(parent.poll())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"grandchild {child} outlived the group it was killed with")
        finally:
            parent.stdout.close()
            if parent.poll() is None:  # the assertions failed; do not leak either
                os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
                parent.wait(timeout=5)

    def test_terminate_on_an_already_dead_process_is_quiet(self):
        """A process that exited between the clock firing and the signal."""
        done = subprocess.Popen(["true"], start_new_session=True)
        done.wait()
        pi_runner._terminate(done)  # must not raise


class CommitPreservationCharacterizationTests(unittest.TestCase):
    """Pins `Run.commit` before lm-ka5.2 moves it.

    Named in this bead's acceptance text and previously untested: every
    assertion here was only ever exercised by a real run.  What matters is
    the invariant, not the wording -- a commit is the run's evidence that
    work happened, so the cases that must never lose it (a failing gate, a
    missing handoff, an outcome that is not "ok") are the ones pinned
    hardest.

    These use a real git repository rather than a mock.  `commit()` reaches
    git four ways -- shortstat, add, commit, rev-parse -- and a mock would
    pin the calls rather than the commits they produce.
    """

    def make_run(self, **gate):
        repo = Path(tempfile.mkdtemp()) / "wt"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "lmloop@test")
        self._git(repo, "config", "user.name", "lmloop test")
        (repo / "seed.txt").write_text("seed\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "seed")

        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        cfg["gate"].update(gate)
        run = Run(root, cfg, "objective", run_id="test-run")
        run.worktree = repo
        run.rundir = RunDir(repo, "test-run")
        run.rundir.path.mkdir(parents=True, exist_ok=True)
        run.screen = mock.MagicMock()
        run.model = "test-model"
        return run, repo, gitops.head_commit(repo)

    @staticmethod
    def _git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    @staticmethod
    def _result(outcome="ok", detail=""):
        return SimpleNamespace(outcome=outcome, detail=detail)

    def _message(self, repo, sha):
        return subprocess.run(
            ["git", "log", "-1", "--format=%B", sha],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout

    # -- the commit itself ------------------------------------------------

    def test_a_touched_file_becomes_a_real_commit(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("seed\nchanged\n")
        sha = run.commit(3, "did a thing", self._result(), True, base)
        self.assertIsNotNone(sha)
        self.assertEqual(sha, gitops.head_commit(repo))
        self.assertEqual(1, gitops.commit_count(repo, base))

    def test_an_untouched_worktree_commits_nothing(self):
        run, repo, base = self.make_run()
        self.assertIsNone(run.commit(1, "no work", self._result(), True, base))
        self.assertEqual(0, gitops.commit_count(repo, base))

    def test_a_failed_outcome_still_commits_what_it_has(self):
        """The whole point: work survives a bad iteration.  An outcome of
        "timeout" or "error" is not a reason to leave the tree dirty."""
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("half-finished\n")
        sha = run.commit(2, "ran out of time", self._result("timeout", "stalled"), False, base)
        self.assertIsNotNone(sha)
        self.assertIn("outcome: timeout (stalled)", self._message(repo, sha))

    def test_new_files_are_committed_even_though_the_shortstat_misses_them(self):
        """Characterises lm-7vq.  `commit_all` runs `git add -A`, so a brand
        new file IS committed -- but `diff_shortstat` is computed first and
        cannot see untracked files, so the `files:` line is missing from the
        body.  Nothing is lost; the record just under-describes it."""
        run, repo, base = self.make_run()
        (repo / "brand_new.py").write_text("x = 1\n")
        sha = run.commit(1, "wrote a module", self._result(), True, base)
        self.assertIsNotNone(sha)
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", sha],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertIn("brand_new.py", tracked)
        self.assertNotIn("files:", self._message(repo, sha))

    # -- the message ------------------------------------------------------

    def test_subject_names_the_run_and_the_iteration(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(7, "did a thing", self._result(), True, base)
        self.assertTrue(
            self._message(repo, sha).startswith("lmloop test-run iter 7: did a thing"),
        )

    def test_a_multiline_summary_is_collapsed_to_one_line(self):
        """Line 1 of the handoff is written by a model and is not reliably
        one line's worth of text."""
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "did\na  thing\n  across lines", self._result(), True, base)
        self.assertTrue(
            self._message(repo, sha).startswith("lmloop test-run iter 1: did a thing across lines"),
        )

    def test_an_overlong_subject_is_trimmed_with_an_ellipsis(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "z" * 200, self._result(), True, base)
        subject = self._message(repo, sha).splitlines()[0]
        self.assertTrue(subject.endswith("..."), subject)
        self.assertEqual("lmloop test-run iter 1: " + "z" * 65 + "...", subject)

    def test_body_records_the_model(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "s", self._result(), True, base)
        self.assertIn("model:   test-model", self._message(repo, sha))

    def test_a_modified_file_is_reported_in_the_files_line(self):
        """Note the padding: every other label in the body is padded to nine
        columns (`outcome: `, `model:   `, `gate:    `, `handoff: `) and
        `files:  ` is padded to eight, so it sits one column left of the
        rest.  Cosmetic, and pinned here as-is rather than quietly fixed --
        this slice is not supposed to change what a commit looks like."""
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("seed\nplus one\n")
        sha = run.commit(1, "s", self._result(), True, base)
        self.assertIn("files:  1 file changed, 1 insertion(+)", self._message(repo, sha))

    def test_a_missing_handoff_is_confessed_in_the_body(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "s", self._result(), False, base)
        self.assertIn("handoff: missing (synthesised from git)", self._message(repo, sha))

    def test_a_written_handoff_is_not_mentioned(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "s", self._result(), True, base)
        self.assertNotIn("handoff:", self._message(repo, sha))

    # -- the gate ---------------------------------------------------------

    def test_a_configured_gate_is_recorded_in_the_body(self):
        run, repo, base = self.make_run(command="make test")
        run.gate_result = "pass"
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(1, "s", self._result(), True, base)
        self.assertIn("gate:    make test -> pass", self._message(repo, sha))

    def test_a_failing_gate_that_does_not_block_still_commits(self):
        run, repo, base = self.make_run(command="make test", blocks_commit=False)
        run.gate_result = "fail (2 tests)"
        (repo / "seed.txt").write_text("x\n")
        self.assertIsNotNone(run.commit(1, "s", self._result(), True, base))

    def test_a_failing_gate_that_blocks_leaves_the_work_in_the_tree(self):
        """`blocks_commit` withholds the commit -- it must never discard the
        work.  The edit stays on disk for the next iteration or a human."""
        run, repo, base = self.make_run(command="make test", blocks_commit=True)
        run.gate_result = "fail (2 tests)"
        (repo / "seed.txt").write_text("precious\n")
        self.assertIsNone(run.commit(4, "s", self._result(), True, base))
        self.assertEqual(0, gitops.commit_count(repo, base))
        self.assertEqual("precious\n", (repo / "seed.txt").read_text())
        self.assertTrue(gitops.has_uncommitted(repo))

    def test_a_blocked_commit_says_so_in_the_event_log(self):
        run, repo, base = self.make_run(command="make test", blocks_commit=True)
        run.gate_result = "fail (2 tests)"
        (repo / "seed.txt").write_text("x\n")
        run.commit(4, "s", self._result(), True, base)
        events = [e for e in run.rundir.read_events() if e.get("event") == "git:commit:blocked"]
        self.assertEqual(1, len(events))
        self.assertEqual(4, events[0]["iteration"])
        self.assertEqual("fail (2 tests)", events[0]["gate"])

    def test_a_passing_gate_that_blocks_commits_normally(self):
        run, repo, base = self.make_run(command="make test", blocks_commit=True)
        run.gate_result = "pass"
        (repo / "seed.txt").write_text("x\n")
        self.assertIsNotNone(run.commit(1, "s", self._result(), True, base))

    def test_a_successful_commit_is_announced_in_the_event_log(self):
        run, repo, base = self.make_run()
        (repo / "seed.txt").write_text("x\n")
        sha = run.commit(5, "s", self._result(), True, base)
        events = [e for e in run.rundir.read_events() if e.get("event") == "git:commit"]
        self.assertEqual(1, len(events))
        self.assertEqual(sha, events[0]["sha"])
        self.assertEqual(5, events[0]["iteration"])
        self.assertEqual("ok", events[0]["outcome"])


class InterruptCharacterizationTests(unittest.TestCase):
    """Pins `Run._on_interrupt` before lm-ka5.2 moves it.

    Named in this bead's acceptance text and previously untested.  The
    contract is two-stage on purpose: the first signal asks the iteration to
    wind up so its work reaches a commit, and only a second one takes the
    process out from under it.
    """

    def make_run(self):
        root = Path(tempfile.mkdtemp())
        run = Run(root, config.load(root), "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        run.screen = mock.MagicMock()
        return run

    def test_a_run_does_not_start_interrupted(self):
        self.assertFalse(self.make_run().interrupted)

    def test_the_first_signal_asks_to_wind_up_rather_than_raising(self):
        run = self.make_run()
        run._on_interrupt(signal.SIGINT, None)  # must not raise
        self.assertTrue(run.interrupted)

    def test_the_first_signal_promises_to_commit_what_it_has(self):
        """The wording is load-bearing: an earlier version promised the
        opposite of what the code did."""
        run = self.make_run()
        run._on_interrupt(signal.SIGINT, None)
        said = " ".join(str(c.args[0]) for c in run.screen.log.call_args_list)
        self.assertIn("committing what it has", said)

    def test_a_second_signal_raises(self):
        run = self.make_run()
        run._on_interrupt(signal.SIGINT, None)
        with self.assertRaises(KeyboardInterrupt):
            run._on_interrupt(signal.SIGINT, None)

    def test_sigterm_is_treated_the_same_as_sigint(self):
        run = self.make_run()
        run._on_interrupt(signal.SIGTERM, None)
        self.assertTrue(run.interrupted)
        with self.assertRaises(KeyboardInterrupt):
            run._on_interrupt(signal.SIGTERM, None)

    def test_an_interrupted_run_stops_sleeping(self):
        """The flag is only worth anything if the waits actually watch it."""
        run = self.make_run()
        run._on_interrupt(signal.SIGINT, None)
        with mock.patch("time.sleep") as slept:
            self.assertFalse(run._sleep_interruptibly(60))
        slept.assert_not_called()


class FinalisationCharacterizationTests(unittest.TestCase):
    """Pins `Run.start`'s exit path, per lm-ka5.8.

    The clean-stop order is characterisation: it is what a run has always
    done and the guarded path must keep doing it.  Everything after that is
    the bug -- before `_finalise` existed, `start` only handled
    `PreflightError`, so any other exception out of `iterate` skipped the
    `run:complete` event, the terminal status, the run-state save, the claim
    release, the screen cleanup and the notification together, and left the
    run reading as "working" with its `loop.pid` still on disk.

    `signal.signal` is patched out throughout: `start` installs real process
    handlers, and a test that let it would leave the runner's own SIGINT
    pointing at a dead `Run`.
    """

    STEPS = ["run:complete", "terminal", "save_state", "release",
             "screen_close", "summarise", "sweep", "announce"]

    def make_run(self):
        repo = Path(tempfile.mkdtemp()) / "wt"
        repo.mkdir()
        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "lmloop@test"),
            ("config", "user.name", "lmloop test"),
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, capture_output=True)

        root = Path(tempfile.mkdtemp())
        run = Run(root, config.load(root), "objective", run_id="test-run")
        run.worktree = repo
        run.rundir = RunDir(repo, "test-run")
        run.rundir.create("objective", gitops.head_commit(repo))
        run.screen = mock.MagicMock()
        return run, repo

    @contextlib.contextmanager
    def driving(self, run, iterate, stop_after=1):
        """Run `start` with the world held still around it.

        Only `iterate` and the stop decision are real inputs; the notify and
        prune tails are mocked out because neither is what these tests are
        about and both reach outside the process.
        """
        with mock.patch.object(loop.signal, "signal"), \
             mock.patch.object(loop.display, "Keys"), \
             mock.patch.object(loop.display, "wait_while_paused"), \
             mock.patch.object(run, "iterate", side_effect=iterate), \
             mock.patch.object(run, "_transport_failure", return_value=""), \
             mock.patch.object(
                 run, "_abort_reason",
                 side_effect=lambda i, s: "asked to stop" if i > stop_after else None), \
             mock.patch.object(run, "_summarise"), \
             mock.patch.object(run, "_sweep"), \
             mock.patch.object(run, "_announce"):
            yield

    def record_steps(self, run):
        """Patch each finalisation step to append its name to one list."""
        seen: list[str] = []
        real_event = run.rundir.event

        def event(name, **fields):
            if name == "run:complete":
                seen.append("run:complete")
            return real_event(name, **fields)

        patches = [
            mock.patch.object(run.rundir, "event", side_effect=event),
            mock.patch.object(run.rundir, "write_terminal_status",
                              side_effect=lambda *a, **k: seen.append("terminal")),
            mock.patch.object(run, "_save_run_state",
                              side_effect=lambda *a, **k: seen.append("save_state")),
            mock.patch.object(run.rundir, "release",
                              side_effect=lambda *a, **k: seen.append("release")),
            mock.patch.object(run.screen, "close",
                              side_effect=lambda *a, **k: seen.append("screen_close")),
            mock.patch.object(run, "_summarise",
                              side_effect=lambda *a, **k: seen.append("summarise")),
            mock.patch.object(run, "_sweep",
                              side_effect=lambda *a, **k: seen.append("sweep")),
            mock.patch.object(run, "_announce",
                              side_effect=lambda *a, **k: seen.append("announce")),
        ]
        return seen, patches

    @staticmethod
    def status(run):
        return json.loads(run.rundir.status_path.read_text())

    @staticmethod
    def events(run, name):
        return [e for e in run.rundir.read_events() if e.get("event") == name]

    # -- the clean stop, unchanged ----------------------------------------

    def test_a_clean_stop_finalises_in_the_established_order(self):
        run, _ = self.make_run()
        seen, patches = self.record_steps(run)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(loop.signal, "signal"))
            stack.enter_context(mock.patch.object(loop.display, "Keys"))
            stack.enter_context(mock.patch.object(loop.display, "wait_while_paused"))
            stack.enter_context(mock.patch.object(run, "iterate"))
            stack.enter_context(mock.patch.object(run, "_transport_failure", return_value=""))
            stack.enter_context(mock.patch.object(
                run, "_abort_reason",
                side_effect=lambda i, s: "asked to stop" if i > 1 else None))
            self.assertEqual(0, run.start())
        self.assertEqual(self.STEPS, seen)

    def test_a_clean_stop_records_its_reason(self):
        run, _ = self.make_run()
        with self.driving(run, lambda n: None):
            run.start()
        self.assertEqual("stopped", self.status(run)["phase"])
        self.assertEqual("asked to stop", self.status(run)["stop_reason"])
        self.assertEqual("asked to stop", self.events(run, "run:complete")[0]["status"])

    def test_a_run_that_stops_on_a_finished_plan_reads_as_completed(self):
        run, _ = self.make_run()
        with mock.patch.object(loop.signal, "signal"), \
             mock.patch.object(loop.display, "Keys"), \
             mock.patch.object(loop.display, "wait_while_paused"), \
             mock.patch.object(run, "iterate"), \
             mock.patch.object(run, "_transport_failure", return_value=""), \
             mock.patch.object(run, "_summarise"), mock.patch.object(run, "_sweep"), \
             mock.patch.object(run, "_announce"), \
             mock.patch.object(run, "_abort_reason",
                               side_effect=lambda i, s: "plan complete" if i > 1 else None):
            run.start()
        self.assertEqual("completed", self.status(run)["phase"])

    # -- the crash, which used to finalise nothing ------------------------

    def test_an_unexpected_exception_still_finalises_every_step(self):
        run, _ = self.make_run()
        seen, patches = self.record_steps(run)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(loop.signal, "signal"))
            stack.enter_context(mock.patch.object(loop.display, "Keys"))
            stack.enter_context(mock.patch.object(loop.display, "wait_while_paused"))
            stack.enter_context(mock.patch.object(run, "_abort_reason", return_value=None))
            stack.enter_context(mock.patch.object(
                run, "iterate", side_effect=RuntimeError("harness exploded")))
            with self.assertRaises(RuntimeError):
                run.start()
        self.assertEqual(self.STEPS, seen)

    def test_an_unexpected_exception_is_re_raised_not_swallowed(self):
        """The operator's exit code and traceback are their copy of the news."""
        run, _ = self.make_run()
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                run.start()
        self.assertEqual("harness exploded", str(caught.exception))

    def test_a_crash_writes_a_terminal_status_that_names_it(self):
        run, _ = self.make_run()
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        status = self.status(run)
        self.assertEqual("stopped", status["phase"])
        self.assertEqual("crashed: RuntimeError: harness exploded", status["stop_reason"])
        self.assertFalse(status["stopping"])

    def test_a_crash_records_run_complete_with_the_same_reason(self):
        run, _ = self.make_run()
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        complete = self.events(run, "run:complete")
        self.assertEqual(1, len(complete))
        self.assertEqual("crashed: RuntimeError: harness exploded", complete[0]["status"])

    def test_a_crash_releases_the_claim(self):
        """A held claim outlives the process that held it: the next run sees
        a `loop.pid` and has to reason about whether it is real."""
        run, _ = self.make_run()
        run.rundir.claim()
        self.assertTrue(run.rundir.pid_path.exists())
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        self.assertFalse(run.rundir.pid_path.exists())

    def test_a_crash_hands_the_terminal_back(self):
        run, _ = self.make_run()
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        run.screen.close.assert_called_once()

    def test_a_crash_never_discards_the_work(self):
        """Invariant 1.  Finalising a crashed run must not be the thing that
        costs it what it managed to produce."""
        run, repo = self.make_run()
        (repo / "precious.py").write_text("hours of work\n")
        (repo / "seed.txt").write_text("edited\n")
        with self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        self.assertEqual("hours of work\n", (repo / "precious.py").read_text())
        self.assertEqual("edited\n", (repo / "seed.txt").read_text())

    def test_finalising_runs_no_destructive_git_command(self):
        """The same invariant, stated against git rather than the tree, so a
        future `_finalise` that tidies up cannot pass by leaving the files
        alone and resetting the index."""
        run, _ = self.make_run()
        seen: list[list[str]] = []
        real_git = gitops.git

        def spy(args, cwd, check=True):
            seen.append(list(args))
            return real_git(args, cwd, check=check)

        with mock.patch.object(gitops, "git", side_effect=spy), \
             self.driving(run, RuntimeError("harness exploded")), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(RuntimeError):
                run.start()
        self.assertTrue(seen, "expected finalisation to touch git at all")
        for args in seen:
            self.assertNotIn(args[0], ("reset", "clean", "checkout", "restore"), args)

    def test_a_second_interrupt_finalises_as_interrupted_rather_than_crashed(self):
        """A second Ctrl-C raises `KeyboardInterrupt` out of `iterate`.  That
        is the operator, not a defect, and it reads as such."""
        run, _ = self.make_run()
        with self.driving(run, KeyboardInterrupt()), \
             mock.patch.object(run, "_abort_reason", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                run.start()
        self.assertEqual("interrupted", self.status(run)["stop_reason"])
        self.assertEqual("interrupted", self.events(run, "run:complete")[0]["status"])

    def test_a_preflight_error_that_gives_up_is_still_an_ordinary_stop(self):
        """`PreflightError` was always handled, and stays handled: it names
        its own reason and must not be relabelled a crash."""
        run, _ = self.make_run()
        with self.driving(run, loop.PreflightError("no model")), \
             mock.patch.object(run, "_abort_reason", return_value=None), \
             mock.patch.object(run, "_backoff", return_value=False):
            self.assertEqual(0, run.start())
        self.assertEqual("preflight failed: no model", self.status(run)["stop_reason"])

    # -- failures inside finalisation itself ------------------------------

    def test_a_missing_base_commit_does_not_cost_the_release(self):
        """`rundir.base_commit` reads a file and raises if it is gone, and it
        sits inside the *first* finalisation step -- which before the guards
        was enough on its own to skip the terminal status and the release."""
        run, _ = self.make_run()
        run.rundir.claim()
        (run.rundir.path / "base-commit").unlink()
        with self.driving(run, lambda n: None):
            self.assertEqual(0, run.start())
        self.assertFalse(run.rundir.pid_path.exists())
        self.assertEqual("asked to stop", self.status(run)["stop_reason"])

    def test_a_failing_step_is_recorded_against_its_name(self):
        run, _ = self.make_run()
        (run.rundir.path / "base-commit").unlink()
        with self.driving(run, lambda n: None):
            run.start()
        failures = self.events(run, "finalise:failed")
        self.assertEqual(1, len(failures))
        self.assertEqual("run:complete event", failures[0]["step"])
        self.assertIn("FileNotFoundError", failures[0]["detail"])

    def test_a_failing_terminal_status_does_not_cost_the_release(self):
        run, _ = self.make_run()
        run.rundir.claim()
        with self.driving(run, lambda n: None), \
             mock.patch.object(run.rundir, "write_terminal_status",
                               side_effect=OSError("disk full")):
            self.assertEqual(0, run.start())
        self.assertFalse(run.rundir.pid_path.exists())
        run.screen.close.assert_called_once()

    def test_a_failing_release_does_not_cost_the_screen(self):
        run, _ = self.make_run()
        with self.driving(run, lambda n: None), \
             mock.patch.object(run.rundir, "release", side_effect=OSError("nope")):
            self.assertEqual(0, run.start())
        run.screen.close.assert_called_once()

    def test_a_broken_event_log_still_lets_the_run_finish(self):
        """The log is one of the things that can be broken at this point, so
        `_safely` guards its own reporting too."""
        run, _ = self.make_run()
        run.rundir.claim()
        with self.driving(run, lambda n: None), \
             mock.patch.object(run.rundir, "event", side_effect=OSError("disk full")):
            self.assertEqual(0, run.start())
        self.assertFalse(run.rundir.pid_path.exists())
        run.screen.close.assert_called_once()

    def test_every_later_step_still_runs_when_an_early_one_fails(self):
        run, _ = self.make_run()
        seen, patches = self.record_steps(run)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(loop.signal, "signal"))
            stack.enter_context(mock.patch.object(loop.display, "Keys"))
            stack.enter_context(mock.patch.object(loop.display, "wait_while_paused"))
            stack.enter_context(mock.patch.object(run, "iterate"))
            stack.enter_context(mock.patch.object(run, "_transport_failure", return_value=""))
            stack.enter_context(mock.patch.object(
                run, "_abort_reason",
                side_effect=lambda i, s: "asked to stop" if i > 1 else None))
            stack.enter_context(mock.patch.object(
                run, "_save_run_state", side_effect=OSError("disk full")))
            run.start()
        self.assertEqual([s for s in self.STEPS if s != "save_state"], seen)


class RunEnvironmentTests(unittest.TestCase):
    """`Run.env` is what the agent process *and* the gate command get, so this
    is the one place a host credential could reach either.  See `env.py`."""

    HOST = {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "GITHUB_TOKEN": "leak",
        "PI_CODING_AGENT_DIR": "/scratch/pi",
        "OMP_THREADS": "8",
        "OPENCODE_HOME": "/oc",
        "SOMEONES_UNRELATED_VAR": "x",
    }

    def make_run(self, harness_name="pi"):
        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        cfg["agent"]["harness"] = harness_name
        run = Run(root, cfg, "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        run.screen = mock.MagicMock()
        return run

    def env_of(self, run):
        with mock.patch.dict(os.environ, self.HOST, clear=True):
            return run.env()

    def test_unrelated_host_credentials_never_reach_the_agent(self):
        kept = self.env_of(self.make_run())
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kept)
        self.assertNotIn("GITHUB_TOKEN", kept)

    def test_the_essentials_still_reach_it(self):
        kept = self.env_of(self.make_run())
        self.assertEqual("/usr/bin", kept["PATH"])
        self.assertEqual("/home/dev", kept["HOME"])

    def test_the_bytecode_redirect_survives_the_allowlist(self):
        """It is an override, not something inherited -- and losing it puts
        `__pycache__` into the agent's next commit."""
        run = self.make_run()
        kept = self.env_of(run)
        self.assertEqual(str(run.rundir.path / "pycache"), kept["PYTHONPYCACHEPREFIX"])

    def test_each_harness_gets_its_own_namespace_and_not_its_siblings(self):
        pi = self.env_of(self.make_run("pi"))
        self.assertIn("PI_CODING_AGENT_DIR", pi)
        self.assertNotIn("OPENCODE_HOME", pi)

        omp = self.env_of(self.make_run("omp"))
        self.assertIn("OMP_THREADS", omp)
        self.assertIn("PI_CODING_AGENT_DIR", omp, "omp is a pi fork and reads PI_* too")

        opencode = self.env_of(self.make_run("opencode"))
        self.assertIn("OPENCODE_HOME", opencode)
        self.assertNotIn("PI_CODING_AGENT_DIR", opencode)

    def test_an_operator_can_opt_a_credential_in(self):
        run = self.make_run()
        run.config["env"]["pass"] = ["GITHUB_TOKEN"]
        kept = self.env_of(run)
        self.assertEqual("leak", kept["GITHUB_TOKEN"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kept)

    def test_an_operator_can_have_the_old_behaviour_back(self):
        run = self.make_run()
        run.config["env"]["inherit"] = "all"
        self.assertIn("AWS_SECRET_ACCESS_KEY", self.env_of(run))

    def test_block_holds_even_against_inherit_all(self):
        run = self.make_run()
        run.config["env"]["inherit"] = "all"
        run.config["env"]["block"] = ["AWS_*"]
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", self.env_of(run))


class ProbeEnvTests(unittest.TestCase):
    """The one-line notice that stops a withheld credential looking like a
    model failure an hour later."""

    def make_run(self):
        root = Path(tempfile.mkdtemp())
        run = Run(root, config.load(root), "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        run.screen = mock.MagicMock()
        return run

    def probe(self, run, host):
        with mock.patch.dict(os.environ, host, clear=True):
            run.probe_env()

    def test_withheld_credentials_are_named_in_the_event_log(self):
        run = self.make_run()
        self.probe(run, {"PATH": "/bin", "GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y"})
        events = [e for e in run.rundir.read_events() if e.get("event") == "env:withheld"]
        self.assertEqual(1, len(events))
        self.assertEqual(2, events[0]["count"])
        self.assertEqual(["AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"], events[0]["names"])

    def test_the_values_are_never_recorded(self):
        run = self.make_run()
        self.probe(run, {"GITHUB_TOKEN": "ghp_realsecret"})
        self.assertNotIn("ghp_realsecret", run.rundir.log_path.read_text())
        said = " ".join(str(c.args[0]) for c in run.screen.log.call_args_list)
        self.assertNotIn("ghp_realsecret", said)

    def test_it_says_how_to_opt_one_in(self):
        run = self.make_run()
        self.probe(run, {"GITHUB_TOKEN": "x"})
        said = " ".join(str(c.args[0]) for c in run.screen.log.call_args_list)
        self.assertIn("GITHUB_TOKEN", said)
        self.assertIn("[env] pass", said)

    def test_a_clean_environment_says_nothing(self):
        run = self.make_run()
        self.probe(run, {"PATH": "/bin", "HOME": "/h"})
        self.assertEqual([], [e for e in run.rundir.read_events()
                              if e.get("event") == "env:withheld"])
        run.screen.log.assert_not_called()

    def test_it_says_nothing_when_the_operator_chose_to_inherit_everything(self):
        run = self.make_run()
        run.config["env"]["inherit"] = "all"
        self.probe(run, {"GITHUB_TOKEN": "x"})
        run.screen.log.assert_not_called()

    def test_a_credential_that_was_opted_in_is_not_reported_as_withheld(self):
        run = self.make_run()
        run.config["env"]["pass"] = ["GITHUB_TOKEN"]
        self.probe(run, {"GITHUB_TOKEN": "x"})
        run.screen.log.assert_not_called()

    def test_a_shell_full_of_credentials_does_not_flood_the_header(self):
        run = self.make_run()
        host = {f"THING_{n}_TOKEN": "x" for n in range(60)}
        self.probe(run, host)
        event = [e for e in run.rundir.read_events() if e.get("event") == "env:withheld"][0]
        self.assertEqual(60, event["count"])
        self.assertEqual(40, len(event["names"]))
        said = " ".join(str(c.args[0]) for c in run.screen.log.call_args_list)
        self.assertIn("and 57 more", said)


class HarnessCapabilityTests(unittest.TestCase):
    """The adapter interface, per lm-ka5.3.

    Each of these replaced a provider-name check in code that has no business
    knowing agent names: `agent_name == "omp"` chose the tool defaults,
    `harness_name != "omp"` decided whether to preflight a browser, and
    `lmloop models` shelled out to `pi --list-models` whatever was configured.
    """

    def adapters(self):
        return [harness.get(name) for name in ("pi", "omp", "opencode")]

    def test_every_bundled_adapter_declares_the_whole_interface(self):
        for adapter in self.adapters():
            self.assertIsInstance(adapter.name, str, adapter)
            self.assertIsInstance(adapter.binary, str, adapter)
            self.assertIsInstance(adapter.default_tools, str, adapter)
            self.assertIsInstance(adapter.browser_tool, str, adapter)
            self.assertIsInstance(adapter.env_passthrough, tuple, adapter)
            self.assertIsInstance(adapter.list_models_argv(), list, adapter)

    def test_only_omp_declares_a_browser(self):
        self.assertEqual("browser", harness.get("omp").browser_tool)
        self.assertEqual("", harness.get("pi").browser_tool)
        self.assertEqual("", harness.get("opencode").browser_tool)

    def test_only_omp_overrides_the_default_tool_allowlist(self):
        """pi's names are what `config.DEFAULTS` was written from, and opencode
        takes no allowlist at all, so neither needs its own."""
        self.assertEqual(harness.OMP_DEFAULT_TOOLS, harness.get("omp").default_tools)
        self.assertEqual("", harness.get("pi").default_tools)
        self.assertEqual("", harness.get("opencode").default_tools)

    def test_model_listing_is_the_adapters_to_declare(self):
        self.assertEqual(["pi", "--list-models"], harness.get("pi").list_models_argv())
        # Not pi's flag, though omp is a pi fork: omp rejects `--list-models`
        # outright and has a `models` subcommand.  Verified against v17.4.0.
        self.assertEqual(["omp", "models"], harness.get("omp").list_models_argv())
        self.assertEqual([], harness.get("opencode").list_models_argv())

    def test_each_adapter_claims_its_own_environment_namespace(self):
        self.assertEqual(("PI_*",), harness.get("pi").env_passthrough)
        self.assertEqual(("OPENCODE_*",), harness.get("opencode").env_passthrough)
        self.assertIn("OMP_*", harness.get("omp").env_passthrough)
        self.assertIn("PI_*", harness.get("omp").env_passthrough)


class ThirdPartyAdapterTests(unittest.TestCase):
    """lm-ka5.3's acceptance in one test class: an adapter nobody shipped goes
    through the core paths without any of them being edited to know its name.

    `Impostor` is defined here, registered for the length of a test, and never
    mentioned anywhere in the codebase.
    """

    class Impostor(harness.Harness):
        name = "impostor"
        binary = "impostor-cli"
        default_tools = "peer,poke"
        browser_tool = "looking-glass"
        env_passthrough = ("IMPOSTOR_*",)

        def list_models_argv(self):
            return [self.binary, "models", "--json"]

        def argv(self, *, model, tools, thinking, session_dir, session_id):
            return [self.binary, "--model", model]

        def classify(self, event):
            return None

    @contextlib.contextmanager
    def registered(self):
        adapter = self.Impostor()
        harness._HARNESSES[adapter.name] = adapter
        try:
            yield adapter
        finally:
            del harness._HARNESSES[adapter.name]

    def test_its_tool_defaults_are_honoured_without_config_knowing_it(self):
        with self.registered():
            self.assertEqual(
                "peer,poke",
                config.resolve_tools("impostor", config.DEFAULTS["agent"]["tools"]),
            )

    def test_an_allowlist_the_operator_typed_is_still_left_alone(self):
        with self.registered():
            self.assertEqual("read,edit", config.resolve_tools("impostor", "read,edit"))

    def test_its_environment_namespace_reaches_the_agent(self):
        with self.registered():
            root = Path(tempfile.mkdtemp())
            cfg = config.load(root)
            cfg["agent"]["harness"] = "impostor"
            run = Run(root, cfg, "objective", run_id="test-run")
            run.rundir.path.mkdir(parents=True)
            host = {"PATH": "/bin", "IMPOSTOR_HOME": "/i", "PI_CODING_AGENT_DIR": "/p"}
            with mock.patch.dict(os.environ, host, clear=True):
                kept = run.env()
        self.assertIn("IMPOSTOR_HOME", kept)
        self.assertNotIn("PI_CODING_AGENT_DIR", kept, "another agent's namespace")

    def test_its_browser_is_preflighted_under_its_own_tool_name(self):
        with self.registered():
            root = Path(tempfile.mkdtemp())
            cfg = config.load(root)
            cfg["agent"]["harness"] = "impostor"
            cfg["agent"]["tools"] = "peer,looking-glass"
            cfg["agent"]["browser_cdp_url"] = "http://127.0.0.1:9222"
            run = Run(root, cfg, "objective", run_id="test-run")
            run.rundir.path.mkdir(parents=True)
            run.screen = mock.MagicMock()
            with mock.patch.object(loop.browser, "preflight",
                                   return_value=(True, "attached")) as preflight:
                run.probe_browser()
        preflight.assert_called_once_with("http://127.0.0.1:9222")

    def test_a_browser_left_out_of_its_allowlist_is_not_preflighted(self):
        with self.registered():
            root = Path(tempfile.mkdtemp())
            cfg = config.load(root)
            cfg["agent"]["harness"] = "impostor"
            cfg["agent"]["tools"] = "peer"
            cfg["agent"]["browser_cdp_url"] = "http://127.0.0.1:9222"
            run = Run(root, cfg, "objective", run_id="test-run")
            run.rundir.path.mkdir(parents=True)
            run.screen = mock.MagicMock()
            with mock.patch.object(loop.browser, "preflight") as preflight:
                run.probe_browser()
        preflight.assert_not_called()

    def test_an_agent_with_no_browser_is_never_preflighted(self):
        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        cfg["agent"]["harness"] = "pi"
        cfg["agent"]["browser_cdp_url"] = "http://127.0.0.1:9222"
        run = Run(root, cfg, "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        run.screen = mock.MagicMock()
        with mock.patch.object(loop.browser, "preflight") as preflight:
            run.probe_browser()
        preflight.assert_not_called()


class LocalServerWaitTests(unittest.TestCase):
    """Whether a failing run waits for a local model server, per lm-cpz.

    The long wait is right for a local server stopped by hand -- the machine's
    owner wanted the GPU, and waiting hours is exactly correct.  It is wrong
    for a model that never touches that server, and asking regardless cost a
    cloud-model run six hours to reach a failure the short backoff reaches in
    seven minutes.
    """

    def make_run(self, model):
        root = Path(tempfile.mkdtemp())
        cfg = config.load(root)
        cfg["agent"]["model"] = model
        run = Run(root, cfg, "objective", run_id="test-run")
        run.rundir.path.mkdir(parents=True)
        run.screen = mock.MagicMock()
        return run

    def test_a_cloud_model_never_waits_for_a_server_it_does_not_use(self):
        run = self.make_run("openrouter/anthropic/claude")
        with mock.patch.object(run, "_server_is_up", return_value=False),              mock.patch.object(run, "_wait_for_server") as wait,              mock.patch.object(run, "_sleep_interruptibly", return_value=True) as slept:
            self.assertTrue(run._backoff(1, "agent-error"))
        wait.assert_not_called()
        # The short backoff instead: one 60-second hold, not 720 of 30.
        self.assertEqual(60, slept.call_args.args[0])

    def test_a_cloud_model_does_not_even_ask_whether_the_server_is_up(self):
        """`_server_is_up` is a network call.  A run that cannot care about the
        answer should not be making it on every failure."""
        run = self.make_run("openrouter/anthropic/claude")
        with mock.patch.object(run, "_server_is_up") as up,              mock.patch.object(run, "_sleep_interruptibly", return_value=True):
            run._backoff(1, "agent-error")
        up.assert_not_called()

    def test_a_cloud_model_gives_up_after_the_short_backoff_not_six_hours(self):
        run = self.make_run("openrouter/anthropic/claude")
        with mock.patch.object(run, "_server_is_up", return_value=False),              mock.patch.object(run, "_sleep_interruptibly", return_value=True):
            self.assertTrue(run._backoff(1, "boom"))
            self.assertTrue(run._backoff(1, "boom"))
            self.assertTrue(run._backoff(1, "boom"))
            self.assertFalse(run._backoff(1, "boom"))

    def test_a_local_model_still_waits_when_the_server_is_gone(self):
        run = self.make_run("llama-swap/local-fast")
        with mock.patch.object(run, "_server_is_up", return_value=False),              mock.patch.object(run, "_wait_for_server", return_value=True) as wait:
            self.assertTrue(run._backoff(3, "server down"))
        wait.assert_called_once_with(3, "server down")

    def test_a_local_model_with_a_live_server_still_takes_the_short_backoff(self):
        """A server that answers and misbehaves does not fix itself by being
        waited on."""
        run = self.make_run("llama-swap/local-fast")
        with mock.patch.object(run, "_server_is_up", return_value=True),              mock.patch.object(run, "_wait_for_server") as wait,              mock.patch.object(run, "_sleep_interruptibly", return_value=True) as slept:
            self.assertTrue(run._backoff(1, "bad build"))
        wait.assert_not_called()
        self.assertEqual(60, slept.call_args.args[0])

    def test_with_no_local_provider_configured_nothing_waits(self):
        """An empty `local_providers` is the setting for a machine with no local
        server at all; a llama-swap-shaped model id must not resurrect the wait."""
        run = self.make_run("llama-swap/local-fast")
        policy = dict(models._FALLBACK, local_providers=[])
        with mock.patch.object(models, "budgets", return_value=policy),              mock.patch.object(run, "_server_is_up", return_value=False),              mock.patch.object(run, "_wait_for_server") as wait,              mock.patch.object(run, "_sleep_interruptibly", return_value=True):
            self.assertTrue(run._backoff(1, "agent-error"))
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
