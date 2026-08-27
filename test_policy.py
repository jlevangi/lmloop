"""Direct tests of `policy.py`'s pure functions.

`test_lmloop.AbortReasonAndBudgetCharacterizationTests` already pins these
through `Run._abort_reason`/`._budget` -- the shape a caller actually sees.
These test the same rules directly, with no `Run`, `RunDir`, or filesystem
involved at all, which is the whole point of having extracted them.
"""

import time
import unittest

import policy


class BudgetTests(unittest.TestCase):
    def test_no_plan_yet_extends_one_past_the_current_iteration(self):
        self.assertEqual(
            4, policy.budget(3, 0, 0, iteration_floor=1, iteration_ceiling=99, retry_allowance=5),
        )

    def test_spends_progress_and_adds_slack(self):
        # spent = 7-1 = 6; remaining = 5-3 = 2; +5 slack = 13
        self.assertEqual(
            13, policy.budget(7, 3, 5, iteration_floor=1, iteration_ceiling=999, retry_allowance=5),
        )

    def test_never_shrinks_below_the_floor(self):
        self.assertEqual(
            10, policy.budget(2, 1, 1, iteration_floor=10, iteration_ceiling=999, retry_allowance=5),
        )

    def test_capped_at_the_ceiling(self):
        self.assertEqual(
            8, policy.budget(7, 3, 5, iteration_floor=1, iteration_ceiling=8, retry_allowance=5),
        )


class AbortReasonTests(unittest.TestCase):
    def base_kwargs(self, **overrides):
        kwargs = dict(
            interrupted=False, stop_now_requested=False, stop_requested=False,
            plan_done=0, plan_total=0, max_iterations=20, iteration_ceiling=20,
            elapsed_before=0.0, max_wall_hours=10, no_diff_streak=0,
            no_diff_iterations_limit=3, plan_at_start=None,
        )
        kwargs.update(overrides)
        return kwargs

    def test_interrupted_wins_over_everything(self):
        self.assertEqual(
            "interrupted",
            policy.abort_reason(
                2, time.monotonic(), **self.base_kwargs(interrupted=True, stop_requested=True),
            ),
        )

    def test_stop_now_wins_over_plain_stop(self):
        self.assertEqual(
            "STOP-NOW sentinel present",
            policy.abort_reason(
                2, time.monotonic(), **self.base_kwargs(stop_now_requested=True, stop_requested=True),
            ),
        )

    def test_plan_complete_requires_iteration_past_one(self):
        kwargs = self.base_kwargs(plan_done=2, plan_total=2)
        self.assertIsNone(policy.abort_reason(1, time.monotonic(), **kwargs))
        self.assertEqual("plan complete (2/2)", policy.abort_reason(2, time.monotonic(), **kwargs))

    def test_max_iterations_vs_turn_ceiling(self):
        below = self.base_kwargs(max_iterations=5, iteration_ceiling=20)
        self.assertEqual("max iterations reached (5)", policy.abort_reason(6, time.monotonic(), **below))
        at_ceiling = self.base_kwargs(max_iterations=20, iteration_ceiling=20)
        self.assertEqual("turn ceiling hit", policy.abort_reason(21, time.monotonic(), **at_ceiling))

    def test_no_diff_streak_reasons_with_and_without_plan_context(self):
        no_plan = self.base_kwargs(no_diff_streak=3)
        reason = policy.abort_reason(5, time.monotonic(), **no_plan)
        self.assertIn("no git-visible change in 3 consecutive iterations", reason)
        self.assertNotIn("(", reason)

        advanced = self.base_kwargs(no_diff_streak=3, plan_done=1, plan_total=2, plan_at_start=0)
        self.assertIn("plan advanced 0/2 -> 1/2", policy.abort_reason(5, time.monotonic(), **advanced))

        unmoved = self.base_kwargs(no_diff_streak=3, plan_done=1, plan_total=2, plan_at_start=1)
        self.assertIn("plan still at 1/2", policy.abort_reason(5, time.monotonic(), **unmoved))

    def test_nothing_fires_returns_none(self):
        self.assertIsNone(policy.abort_reason(2, time.monotonic(), **self.base_kwargs()))


class CountsAsNoProgressTests(unittest.TestCase):
    def test_only_ok_no_action_and_truncated_with_no_git_evidence_count(self):
        for outcome in ("agent-error", "timeout", "stalled", "thrashing", "interrupted"):
            self.assertFalse(policy.counts_as_no_progress(outcome, None, False), outcome)
        for outcome in ("ok", "no-action", "truncated"):
            self.assertTrue(policy.counts_as_no_progress(outcome, None, False), outcome)

    def test_a_commit_or_uncommitted_change_is_always_progress(self):
        self.assertFalse(policy.counts_as_no_progress("ok", "abc123", False))
        self.assertFalse(policy.counts_as_no_progress("ok", None, True))


class TransportFailureTests(unittest.TestCase):
    def test_a_matching_agent_error_with_no_commit_is_transport(self):
        self.assertEqual(
            "connection reset",
            policy.transport_failure("agent-error", None, "connection reset"),
        )

    def test_case_insensitive_matching(self):
        self.assertEqual(
            "Bad Gateway", policy.transport_failure("agent-error", None, "Bad Gateway"),
        )

    def test_a_non_agent_error_outcome_is_never_transport(self):
        self.assertEqual("", policy.transport_failure("timeout", None, "connection reset"))

    def test_a_commit_means_the_work_is_kept_not_retried(self):
        self.assertEqual("", policy.transport_failure("agent-error", "abc123", "connection reset"))

    def test_a_genuine_model_failure_has_no_transport_marker(self):
        self.assertEqual(
            "", policy.transport_failure("agent-error", None, "request exceeds the available context size"),
        )


class BackoffDelayTests(unittest.TestCase):
    def test_delay_doubles_each_time_then_gives_up_after_three(self):
        self.assertEqual(60, policy.backoff_delay(1))
        self.assertEqual(120, policy.backoff_delay(2))
        self.assertEqual(240, policy.backoff_delay(3))
        self.assertIsNone(policy.backoff_delay(4))


class FailureReasonTests(unittest.TestCase):
    """How a run that died of an exception describes itself afterwards."""

    def test_a_second_ctrl_c_is_the_operator_not_a_crash(self):
        self.assertEqual("interrupted", policy.failure_reason(KeyboardInterrupt()))

    def test_an_exception_names_its_type_and_message(self):
        self.assertEqual(
            "crashed: RuntimeError: harness exploded",
            policy.failure_reason(RuntimeError("harness exploded")),
        )

    def test_an_exception_with_no_message_still_names_its_type(self):
        self.assertEqual("crashed: ValueError", policy.failure_reason(ValueError()))

    def test_a_multiline_message_is_collapsed(self):
        """This lands in `status.json` and in the closing summary."""
        self.assertEqual(
            "crashed: RuntimeError: line one line two",
            policy.failure_reason(RuntimeError("line one\n  line two")),
        )

    def test_an_enormous_message_is_capped(self):
        reason = policy.failure_reason(RuntimeError("x" * 5000))
        self.assertEqual(200, len(reason))
        self.assertTrue(reason.endswith("..."))

    def test_a_message_just_under_the_cap_is_left_alone(self):
        error = RuntimeError("y" * (200 - len("crashed: RuntimeError: ")))
        self.assertEqual(200, len(policy.failure_reason(error)))
        self.assertFalse(policy.failure_reason(error).endswith("..."))


class NoPlanBudgetTests(unittest.TestCase):
    """The budget when the agent has written no plan, per lm-0l7.

    This branch exists so an empty plan does not derive a budget of one and
    stop the run at the planning iteration -- which the floor already
    prevents.  It was not capped by the ceiling, so `max_iterations` came back
    as `iteration + 1` every time and `iteration > max_iterations` could never
    be true: the turn ceiling was unreachable and the run stopped only when
    the wall clock ran out.  A fake-harness run configured for two iterations
    reached forty-three.
    """

    def budget(self, iteration, floor=2, ceiling=2):
        return policy.budget(
            iteration, 0, 0,
            iteration_floor=floor, iteration_ceiling=ceiling, retry_allowance=5,
        )

    def test_the_planning_iteration_is_never_cut_short(self):
        self.assertGreaterEqual(self.budget(1), 2)

    def test_the_budget_stops_at_the_ceiling(self):
        for iteration in range(1, 12):
            with self.subTest(iteration=iteration):
                self.assertLessEqual(self.budget(iteration), 2)

    def test_the_ceiling_becomes_reachable(self):
        """`abort_reason` stops on `iteration > max_iterations`; if the budget
        always outruns the iteration, that can never happen."""
        reached = any(iteration > self.budget(iteration) for iteration in range(1, 12))
        self.assertTrue(reached, "no iteration ever exceeds its budget")

    def test_the_floor_still_wins_over_a_lower_ceiling_argument(self):
        self.assertEqual(20, self.budget(1, floor=20, ceiling=20))
        self.assertEqual(20, self.budget(15, floor=20, ceiling=20))

    def test_a_run_with_a_plan_is_unaffected(self):
        self.assertEqual(
            policy.budget(3, 1, 4, iteration_floor=2, iteration_ceiling=20,
                          retry_allowance=5),
            2 + 3 + 5,
        )


class ReplyPressureTests(unittest.TestCase):
    """Saying an iteration ran out of room to *answer*, not to read.

    The other budget, and the one nothing watched. `max_output_tokens` applies
    per reply; `output_tokens` is the whole iteration's total, and lmloop
    reported them side by side as though they were the same scale -- 13,747
    out, 24,576 reply cap -- on a run whose largest single reply was 3,535
    tokens, 14% of the cap. Read as a budget half spent when it was barely
    touched.

    And the real failure was the other way round: only the *last* message's
    stop reason reaches the outcome, so a reply cut off mid-iteration left no
    trace at all once the agent recovered and wrote something.
    """

    def test_replies_that_all_fitted_say_nothing(self):
        self.assertEqual("", policy.reply_warning(0, 3535, 24576))

    def test_one_reply_cut_off_says_so(self):
        said = policy.reply_warning(1, 24576, 24576)
        self.assertIn("1 reply", said)
        self.assertIn("24576", said)

    def test_it_counts_them_and_gets_the_plural_right(self):
        self.assertIn("3 replies", policy.reply_warning(3, 24576, 24576))

    def test_it_names_the_budget_rather_than_the_work(self):
        """The fix is a wider cap or less thinking, not a smaller objective --
        the same reason `context_warning` names the model."""
        said = policy.reply_warning(1, 8192, 8192)
        self.assertIn("output budget", said)
        self.assertIn("thinking", said)

    def test_it_is_not_a_threshold(self):
        """A reply either hit the cap or it did not, and the agent says which.
        A percentage here would add a false alarm to a certain signal -- and
        the archive has exactly one truncation to fit one from."""
        # Enormous peak, nothing cut off: silence.
        self.assertEqual("", policy.reply_warning(0, 24575, 24576))
        # Tiny peak, something cut off: still said.
        self.assertNotEqual("", policy.reply_warning(1, 10, 24576))

    def test_an_unknown_cap_still_reports_what_happened(self):
        """`Run.max_output` is 0 for a model nobody measured. That is a reason
        to omit the cap from the sentence, not to swallow the sentence."""
        said = policy.reply_warning(1, 8192, 0)
        self.assertNotEqual("", said)
        self.assertNotIn("0-token", said)


class ContextPressureTests(unittest.TestCase):
    """Saying an iteration is running out of room, before it runs out.

    lmloop recorded `context_window` and `input_tokens` and never compared
    them, so the one number that predicts a compaction was in the run's own
    files and reported nowhere. On a 24,576-token profile, iterations that
    ended in an overflow were sitting at 19,644, 19,921 and 20,599 input
    tokens beforehand; finding that out took reading token counts by hand
    afterwards (lm-oit).
    """

    def test_a_comfortable_prompt_says_nothing(self):
        self.assertEqual("", policy.context_warning(12000, 24576))

    def test_a_prompt_near_the_limit_says_so(self):
        said = policy.context_warning(20599, 24576)
        self.assertIn("84%", said)
        self.assertIn("24576", said)
        self.assertIn("20599", said)

    def test_the_threshold_is_where_the_measurements_put_it(self):
        window = 24576
        self.assertEqual("", policy.context_warning(int(window * 0.74), window))
        self.assertNotEqual("", policy.context_warning(int(window * 0.76), window))

    def test_the_same_prompt_is_fine_on_a_wider_profile(self):
        """Which is the whole point: the fix is usually a wider model, not a
        smaller objective."""
        self.assertNotEqual("", policy.context_warning(20599, 24576))
        self.assertEqual("", policy.context_warning(20599, 106496))

    def test_an_unmeasured_model_invents_nothing(self):
        """`Run.window` is 0 for a model nobody has measured, and a ratio
        against zero would be an invented number rather than a missing one."""
        self.assertEqual("", policy.context_warning(20599, 0))
        self.assertEqual(0.0, policy.context_pressure(20599, 0))

    def test_a_prompt_nobody_counted_invents_nothing_either(self):
        self.assertEqual("", policy.context_warning(0, 24576))

    def test_the_ratio_is_plain_arithmetic(self):
        self.assertAlmostEqual(0.5, policy.context_pressure(100, 200))


if __name__ == "__main__":
    unittest.main()
