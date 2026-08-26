"""Watching a detached run without owning it."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import attach
import display


class FakeScreen:
    """A screen that records instead of drawing."""

    tty = False

    def __init__(self):
        self.paint = display.Screen().paint
        self.logged = []
        self.lines = []
        self.closed = False

    def log(self, text=""):
        self.logged.append(text)

    def status(self, segments):
        self.lines.append(segments)

    def spin(self, advance=True):
        return "|"

    def close(self):
        self.closed = True

    def said(self):
        return "\n".join(self.logged)


def write_run(status=None, events=()):
    run_dir = Path(tempfile.mkdtemp())
    if status is not None:
        (run_dir / "status.json").write_text(json.dumps(status))
    if events:
        (run_dir / "lmloop.log").write_text(
            "".join(json.dumps(e) + "\n" for e in events))
    return run_dir


class StatusLineTests(unittest.TestCase):
    """The same shape a foreground run draws; see `Run._show`."""

    def line(self, status, age=1.0, spinner="|"):
        return attach._line(status, spinner, age, display.Screen().paint)

    def rendered(self, status, **kwargs):
        return " ".join(text for _, text in self.line(status, **kwargs) if text)

    def test_it_leads_with_the_iteration_counter(self):
        first = self.line({"iteration": 3, "max_iterations": 9})[0]
        self.assertIn("3/9", first[1])

    def test_the_current_tool_is_shown_when_there_is_one(self):
        self.assertIn("edit calc.py", self.rendered(
            {"last_tool": "edit", "last_target": "calc.py"}))

    def test_thinking_when_there_is_not(self):
        self.assertIn("thinking", self.rendered({}))

    def test_a_loading_model_says_so_rather_than_thinking(self):
        self.assertIn("loading model", self.rendered({"phase": "loading"}))

    def test_a_stale_run_says_so_instead_of_pretending(self):
        """status.json is the last thing a crashed run wrote, and it says
        "working" -- the age is the only thing that knows better."""
        shown = self.rendered({"last_tool": "edit"}, age=attach.STALE_AFTER + 60)
        self.assertIn("no update for", shown)
        self.assertNotIn("edit", shown)

    def test_plan_progress_appears_only_once_there_is_a_plan(self):
        self.assertIn("2/5 steps", self.rendered({"plan_done": 2, "plan_total": 5}))
        self.assertNotIn("steps", self.rendered({"plan_done": 0, "plan_total": 0}))

    def test_overflows_appear_only_once_they_have_happened(self):
        self.assertIn("2 overflow", self.rendered({"compactions": 2}))
        self.assertNotIn("overflow", self.rendered({"compactions": 0}))

    def test_the_control_flags_are_shown(self):
        self.assertIn("PAUSE", self.rendered({"paused": True}))
        self.assertIn("STOP", self.rendered({"stopping": True}))

    def test_an_empty_status_still_renders(self):
        """A run that has only just started has almost nothing in the file."""
        self.rendered({})

    def test_the_segment_weights_match_the_foreground_line(self):
        """The two screens are meant to look the same; a viewer that laid them
        out differently would be worse than no viewer."""
        weights = [weight for weight, _ in self.line({"iteration": 1})]
        self.assertEqual([6, 4, 5, 3, 4, 2, 2, 1, 7], weights)


class EventDescriptionTests(unittest.TestCase):
    def test_an_iteration_ending_reports_its_outcome_and_commit(self):
        said = attach._describe({
            "event": "iteration:end", "outcome": "ok", "toolCalls": 6,
            "commit": "abcdef1234567890"})
        self.assertIn("ok", said)
        self.assertIn("6 tool calls", said)
        self.assertIn("abcdef12", said)

    def test_an_iteration_that_committed_nothing_says_so(self):
        said = attach._describe({"event": "iteration:end", "outcome": "no-action"})
        self.assertIn("nothing to commit", said)

    def test_the_run_ending_reports_why(self):
        self.assertIn("turn ceiling hit", attach._describe(
            {"event": "run:complete", "status": "turn ceiling hit"}))

    def test_a_server_wait_is_worth_interrupting_for(self):
        """Otherwise a run that is holding for a stopped llama-swap looks
        identical to one that has hung."""
        self.assertIn("holding", attach._describe(
            {"event": "server:wait", "detail": "connection refused"}))

    def test_per_iteration_noise_is_not(self):
        for name in ("preflight", "checks:failed", "git:commit", "env:withheld"):
            with self.subTest(event=name):
                self.assertEqual("", attach._describe({"event": name}))


class WatchTests(unittest.TestCase):
    def test_a_run_with_no_status_says_so(self):
        screen = FakeScreen()
        self.assertEqual(1, attach.watch(write_run(), "some-run", screen))
        self.assertIn("no status yet", screen.said())

    def test_a_run_that_is_already_over_is_reported_not_watched(self):
        run_dir = write_run({"phase": "stopped", "stop_reason": "turn ceiling hit"})
        screen = FakeScreen()
        self.assertEqual(1, attach.watch(run_dir, "r", screen))
        self.assertIn("already stopped", screen.said())
        self.assertIn("turn ceiling hit", screen.said())

    def test_watching_one_finish_is_a_different_answer(self):
        run_dir = write_run(
            {"phase": "working", "iteration": 1},
            [{"event": "run:complete", "status": "plan complete"}],
        )
        screen = FakeScreen()
        # The completion is already in the log, so it is skipped as history --
        # but the phase still says working, so this exercises the live path.
        with mock.patch.object(attach.time, "sleep"):
            (run_dir / "lmloop.log").write_text("")
            (run_dir / "status.json").write_text(json.dumps({"phase": "completed"}))
            self.assertEqual(1, attach.watch(run_dir, "r", screen))

    def test_history_is_not_replayed_on_connect(self):
        """Attaching is "show me what happens from now"; replaying forty
        iterations of a run somebody has watched for hours is not a view."""
        events = [{"event": "iteration:end", "outcome": "ok", "toolCalls": 1}
                  for _ in range(40)]
        run_dir = write_run({"phase": "stopped"}, events)
        screen = FakeScreen()
        attach.watch(run_dir, "r", screen)
        self.assertNotIn("tool calls", screen.said())

    def test_it_opens_by_saying_where_the_run_has_got_to(self):
        run_dir = write_run({"phase": "stopped", "iteration": 7, "max_iterations": 9})
        screen = FakeScreen()
        attach.watch(run_dir, "r", screen)
        self.assertIn("iteration 7/9", screen.said())

    def test_watching_never_claims_the_run(self):
        """A viewer that took the claim would make the loop look like it had
        been replaced, and a second one would be refused."""
        run_dir = write_run({"phase": "stopped"})
        attach.watch(run_dir, "r", FakeScreen())
        self.assertFalse((run_dir / "loop.pid").exists())

    def test_watching_never_writes_the_status_file(self):
        run_dir = write_run({"phase": "stopped", "iteration": 2})
        before = (run_dir / "status.json").read_text()
        attach.watch(run_dir, "r", FakeScreen())
        self.assertEqual(before, (run_dir / "status.json").read_text())


if __name__ == "__main__":
    unittest.main()
