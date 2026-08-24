import unittest
from datetime import datetime
from unittest import mock

import eta


def end(seconds, *, done=0, total=0, outcome="ok"):
    return {
        "event": "iteration:end", "elapsedMs": seconds * 1000,
        "planDone": done, "planTotal": total, "outcome": outcome,
    }


class EstimateTests(unittest.TestCase):
    @mock.patch("eta.datetime")
    def test_plan_eta_uses_median_progress_duration(self, clock):
        clock.now.return_value = datetime(2026, 8, 24, tzinfo=eta.timezone.utc)
        result = eta.estimate([
            end(600, done=1, total=4),
            end(1800, done=2, total=4),
            end(7200, done=2, total=4),  # no plan progress; excluded
        ], elapsed_seconds=300, iteration=4, max_iterations=10,
           plan_done=2, plan_total=4)
        self.assertEqual(2100, result["eta_seconds"])
        self.assertEqual("plan steps", result["eta_basis"])
        self.assertEqual(2, result["eta_samples"])
        self.assertEqual("2026-08-24T00:35:00+00:00", result["eta_at"])

    def test_two_samples_are_required(self):
        self.assertEqual({}, eta.estimate(
            [end(600, done=1, total=3)], iteration=2, max_iterations=5,
            plan_done=1, plan_total=3,
        ))

    def test_pre_plan_fallback_excludes_failed_turns(self):
        result = eta.estimate([
            end(600), end(1200, outcome="truncated"),
            end(9000, outcome="agent-error"),
        ], iteration=3, max_iterations=5)
        self.assertEqual(2700, result["eta_seconds"])
        self.assertEqual("iterations", result["eta_basis"])
        self.assertEqual(2, result["eta_samples"])

    def test_overdue_active_turn_reports_due_now(self):
        result = eta.estimate(
            [end(600, done=1, total=3), end(600, done=2, total=3)],
            elapsed_seconds=900, iteration=3, max_iterations=5,
            plan_done=2, plan_total=3,
        )
        self.assertEqual(60, result["eta_seconds"])

    def test_completed_plan_has_no_eta(self):
        self.assertEqual({}, eta.estimate(
            [end(600, done=1, total=2), end(600, done=2, total=2)],
            iteration=2, max_iterations=5, plan_done=2, plan_total=2,
        ))


if __name__ == "__main__":
    unittest.main()
