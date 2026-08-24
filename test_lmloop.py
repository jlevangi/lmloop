import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import os
import signal
import subprocess
from unittest import mock

import browser
import config
import harness
import pi_runner
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
