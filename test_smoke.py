"""The fake harness, exercised through the real loop.

`tools/smoke` is the full version -- a scratch repository, a gate, two
iterations, and assertions about the commit and the run directory. These are
the parts of it that are cheap enough to keep in the unit suite: that the fake
agent speaks the stream the pi adapter expects, and that each scripted outcome
produces the outcome it is named after.

Reading the adapter proves the first of those only if the adapter is right,
which is the thing being checked.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

import harness

FAKE = Path(__file__).parent / "tools" / "fake-agent"


def run_fake(scenario, cwd):
    if scenario is not None:
        (cwd / ".fake-agent.json").write_text(json.dumps(scenario))
    done = subprocess.run(
        [sys.executable, str(FAKE), "-p", "--mode", "json"],
        input=b"the prompt", capture_output=True, cwd=cwd, timeout=30,
    )
    events = []
    for line in done.stdout.decode().splitlines():
        if line.strip():
            events.append(json.loads(line))
    return done, events


class FakeAgentStreamTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.cwd = Path(tempfile.mkdtemp())

    def test_it_exists_and_is_executable(self):
        self.assertTrue(FAKE.is_file(), FAKE)
        self.assertTrue(FAKE.stat().st_mode & 0o111, "must be executable")

    def test_every_line_it_writes_is_json(self):
        done, events = run_fake(None, self.cwd)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertTrue(events)

    def test_the_pi_adapter_understands_its_stream(self):
        """The whole point: it has to speak what the adapter reads, not what
        it seemed to when this was written."""
        _, events = run_fake(None, self.cwd)
        adapter = harness.get("pi")
        classified = [adapter.classify(event) for event in events]
        kinds = {result["kind"] for result in classified if result}
        self.assertIn(harness.TOOL, kinds)
        self.assertIn(harness.MESSAGE_END, kinds)

    def test_its_events_survive_the_interesting_filter(self):
        """They are matched against raw bytes before anything is parsed."""
        _, events = run_fake(None, self.cwd)
        adapter = harness.get("pi")
        for event in events:
            if adapter.classify(event) is None:
                continue
            line = json.dumps(event)
            self.assertTrue(any(m in line for m in adapter.interesting), line[:80])

    def test_it_really_edits_the_file_it_says_it_did(self):
        run_fake({"write": "out.txt", "append": "written\n"}, self.cwd)
        self.assertEqual("written\n", (self.cwd / "out.txt").read_text())

    def test_it_appends_rather_than_replacing(self):
        (self.cwd / "out.txt").write_text("first\n")
        run_fake({"write": "out.txt", "append": "second\n"}, self.cwd)
        self.assertEqual("first\nsecond\n", (self.cwd / "out.txt").read_text())

    def test_it_writes_a_handoff_into_the_run_directory_it_finds(self):
        """Found rather than told: lmloop passes the run directory in the
        prompt, and a fake agent that parsed the prompt would break every time
        the prompt was reworded."""
        run_dir = self.cwd / ".lmloop" / "runs" / "some-run"
        run_dir.mkdir(parents=True)
        run_fake({"handoff": "did a thing"}, self.cwd)
        self.assertEqual("did a thing\n", (run_dir / "handoff.md").read_text())

    def test_no_run_directory_is_not_an_error(self):
        done, _ = run_fake({"handoff": "did a thing"}, self.cwd)
        self.assertEqual(0, done.returncode)


class ScriptedOutcomeTests(unittest.TestCase):
    """Each scenario must produce the outcome it is named after.

    `truncated` and `no-action` write nothing on purpose: lmloop only calls a
    `length` stop "truncated" when there were no writes, because with writes
    something was produced and `ok` is the honest word.
    """

    def setUp(self):
        import tempfile
        self.cwd = Path(tempfile.mkdtemp())

    def stop_reason_and_tools(self, scenario):
        _, events = run_fake(scenario, self.cwd)
        adapter = harness.get("pi")
        tools = sum(1 for e in events
                    if (adapter.classify(e) or {}).get("kind") == harness.TOOL)
        ends = [adapter.classify(e) for e in events
                if (adapter.classify(e) or {}).get("kind") == harness.MESSAGE_END]
        return (ends[0] if ends else None), tools

    def test_ok_calls_a_tool_and_stops_cleanly(self):
        end, tools = self.stop_reason_and_tools({"outcome": "ok"})
        self.assertEqual("stop", end["stop_reason"])
        self.assertEqual(1, tools)

    def test_error_reports_a_stop_reason_lmloop_treats_as_a_failure(self):
        end, _ = self.stop_reason_and_tools({"outcome": "error"})
        self.assertEqual("error", end["stop_reason"])
        self.assertTrue(end["error"], "an error outcome must carry a message")

    def test_no_action_calls_no_tool_at_all(self):
        end, tools = self.stop_reason_and_tools({"outcome": "no-action"})
        self.assertEqual(0, tools)
        self.assertEqual("stop", end["stop_reason"])

    def test_truncated_is_a_length_stop_with_nothing_written(self):
        end, tools = self.stop_reason_and_tools({"outcome": "truncated"})
        self.assertEqual("length", end["stop_reason"])
        self.assertEqual(0, tools, "a truncated turn never got its tool call out")

    def test_silent_says_nothing_an_adapter_can_use(self):
        """A harness that exits having produced no assistant message; lmloop
        should call that an agent-error rather than a success."""
        _, events = run_fake({"outcome": "silent"}, self.cwd)
        adapter = harness.get("pi")
        self.assertFalse([e for e in events
                          if (adapter.classify(e) or {}).get("kind") == harness.MESSAGE_END])

    def test_an_unreadable_scenario_falls_back_to_the_default(self):
        (self.cwd / ".fake-agent.json").write_text("{not json")
        done, events = run_fake(None, self.cwd)
        self.assertEqual(0, done.returncode)
        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()
