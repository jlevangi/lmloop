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
import harness
import lmloop
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


if __name__ == "__main__":
    unittest.main()
