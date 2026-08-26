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


if __name__ == "__main__":
    unittest.main()
