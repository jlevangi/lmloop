"""What each bundled adapter must make of its agent's real output.

The fixtures in `testdata/` are captured from actual runs -- pi from an
archived run, omp from a live worktree -- and then redacted by a default-deny
pass that replaces every string whose key is not structural, so no source
repository content survives.  What is left is the shape: event names, field
names, roles, stop reasons, and the token counts, which is exactly the part
an adapter has to keep agreeing with.

The point of capturing rather than inventing: an invented event asserts what
the adapter already believes.  These have caught nothing yet because they are
new, but the class of bug they exist for -- an agent renaming a field, or the
two forks drifting apart -- is one this project has already paid for twice
(`auto_compaction_*` vs `compaction_*`, and `--list-models`).
"""

import json
import unittest
from pathlib import Path

import harness

TESTDATA = Path(__file__).parent / "testdata"


def load(name):
    """The captured stream as `{variant: event}`, keyed the way tests ask."""
    events = {}
    for line in (TESTDATA / name).read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message") or {}
            role = message.get("role")
            key = (f"message_end:assistant:{message.get('stopReason')}"
                   if role == "assistant" else f"message_end:{role}")
        else:
            key = kind
        events[key] = event
    return events


class CapturedEventContractTests(unittest.TestCase):
    CASES = (("pi", "pi-events.jsonl", "compaction_start"),
             ("omp", "omp-events.jsonl", "auto_compaction_start"))

    def test_a_tool_call_is_normalised_to_kind_name_and_target(self):
        for agent, fixture, _ in self.CASES:
            with self.subTest(agent=agent):
                adapter = harness.get(agent)
                event = load(fixture)["tool_execution_start"]
                result = adapter.classify(event)
                self.assertEqual(harness.TOOL, result["kind"])
                self.assertEqual(event["toolName"], result["name"])
                self.assertTrue(result["target"], "a tool call with args must have a target")
                self.assertIn("path", result)

    def test_an_assistant_turn_carries_its_stop_reason_and_token_counts(self):
        for agent, fixture, _ in self.CASES:
            with self.subTest(agent=agent):
                adapter = harness.get(agent)
                event = load(fixture)["message_end:assistant:toolUse"]
                result = adapter.classify(event)
                self.assertEqual(harness.MESSAGE_END, result["kind"])
                self.assertEqual("toolUse", result["stop_reason"])
                usage = event["message"]["usage"]
                self.assertEqual(usage["input"], result["input"])
                self.assertEqual(usage["output"], result["output"])
                self.assertGreater(result["input"], 0, "captured usage should be real")

    def test_every_captured_stop_reason_survives_classification(self):
        """`length` is the one that matters most: it is how an overflow shows
        up, and an adapter that dropped it would make a truncated turn look
        like a clean one."""
        for agent, fixture, _ in self.CASES:
            adapter = harness.get(agent)
            events = load(fixture)
            for key, event in events.items():
                if not key.startswith("message_end:assistant:"):
                    continue
                with self.subTest(agent=agent, variant=key):
                    result = adapter.classify(event)
                    self.assertEqual(key.rsplit(":", 1)[1], result["stop_reason"])

    def test_a_tool_result_is_not_mistaken_for_an_assistant_turn(self):
        """Both arrive as `message_end`.  Counting a `toolResult` as a turn
        would multiply the token totals by the number of tools called."""
        for agent, fixture, _ in self.CASES:
            with self.subTest(agent=agent):
                adapter = harness.get(agent)
                events = load(fixture)
                self.assertIsNone(adapter.classify(events["message_end:toolResult"]))
                self.assertIsNone(adapter.classify(events["message_end:user"]))

    def test_each_adapter_recognises_only_its_own_compaction_event(self):
        """The one that has actually bitten: omp names it `auto_compaction_*`
        where pi names it `compaction_*`, and an adapter hardcoded to either
        harvests nothing from the other while saying nothing about it."""
        for agent, fixture, own in self.CASES:
            with self.subTest(agent=agent):
                adapter = harness.get(agent)
                self.assertEqual(own, adapter.compaction_event)
                event = load(fixture)[own]
                self.assertEqual({"kind": harness.COMPACTION}, adapter.classify(event))

        # And crossed over, the other agent's name means nothing.
        self.assertIsNone(
            harness.get("pi").classify(load("omp-events.jsonl")["auto_compaction_start"]),
        )
        self.assertIsNone(
            harness.get("omp").classify(load("pi-events.jsonl")["compaction_start"]),
        )

    def test_bookkeeping_events_classify_to_nothing(self):
        for agent, fixture, _ in self.CASES:
            adapter = harness.get(agent)
            events = load(fixture)
            for key in ("agent_start", "turn_start", "tool_execution_end"):
                if key not in events:
                    continue
                with self.subTest(agent=agent, event=key):
                    self.assertIsNone(adapter.classify(events[key]))

    def test_classify_never_raises_on_any_captured_event(self):
        """It runs on every line of a stream that reaches tens of megabytes."""
        for agent, fixture, _ in self.CASES:
            adapter = harness.get(agent)
            for key, event in load(fixture).items():
                with self.subTest(agent=agent, event=key):
                    adapter.classify(event)


class StreamFilterContractTests(unittest.TestCase):
    """`interesting` and `activity` run against raw bytes before anything is
    parsed, so a name that drifts here silently stops the loop seeing events
    it still handles perfectly well once they arrive."""

    CASES = (("pi", "pi-events.jsonl"), ("omp", "omp-events.jsonl"))

    def test_every_event_the_adapter_classifies_survives_the_interesting_filter(self):
        for agent, fixture in self.CASES:
            adapter = harness.get(agent)
            for key, event in load(fixture).items():
                if adapter.classify(event) is None:
                    continue
                with self.subTest(agent=agent, event=key):
                    line = json.dumps(event)
                    self.assertTrue(
                        any(marker in line for marker in adapter.interesting),
                        f"{key} is classified but would never be parsed",
                    )

    def test_the_activity_markers_appear_in_real_output(self):
        """They are what tells "the model is still loading" from "it hung"."""
        for agent, fixture in self.CASES:
            adapter = harness.get(agent)
            blob = (TESTDATA / fixture).read_bytes()
            with self.subTest(agent=agent):
                self.assertTrue(adapter.activity, "an adapter needs a stall signal")
                for marker in adapter.activity:
                    self.assertIn(marker, blob)

    def test_each_compaction_marker_appears_in_its_own_streams_bytes(self):
        """Scanned against raw bytes before parsing, so it has to match the
        wire form and not the name someone remembered."""
        for agent, fixture in self.CASES:
            with self.subTest(agent=agent):
                marker = harness.get(agent).compaction_marker
                self.assertTrue(marker, "both bundled agents compact")
                self.assertIn(marker, (TESTDATA / fixture).read_bytes())

    def test_neither_compaction_marker_matches_the_other_agents_stream(self):
        """The leading quote in each marker is load-bearing, in both
        directions.  `"auto_compaction_end"` contains `compaction_end`, so
        without its quote pi's marker fires on omp's stream; and without its
        own quote omp's marker (`compaction_end"`) fires on pi's.  Either way
        one agent harvests a summary out of a stream that is not its own.
        """
        streams = {agent: (TESTDATA / fixture).read_bytes()
                   for agent, fixture in self.CASES}
        for agent, _ in self.CASES:
            marker = harness.get(agent).compaction_marker
            for other, blob in streams.items():
                if other == agent:
                    continue
                with self.subTest(marker=agent, stream=other):
                    self.assertNotIn(marker, blob)


class CompactionSummaryContractTests(unittest.TestCase):
    """The summary an agent writes for itself on the way out of an overflow.

    Worth a captured test rather than an invented one: it is buried several
    levels into the event, the two agents put it in differently named events,
    and when the harvest misses the loop silently falls back to a
    git-synthesised handoff -- worse, but never obviously broken.
    """

    def test_each_agent_harvests_its_summary_from_its_own_event(self):
        for agent, fixture, event_name in (
            ("pi", "pi-events.jsonl", "compaction_end"),
            ("omp", "omp-events.jsonl", "auto_compaction_end"),
        ):
            with self.subTest(agent=agent):
                event = load(fixture)[event_name]
                # The fixture's summary is redacted to a placeholder, so a
                # non-empty answer means the adapter found the right field.
                self.assertEqual("<summary>",
                                 harness.get(agent).compaction_summary(event))

    def test_an_event_with_no_summary_harvests_nothing_rather_than_raising(self):
        for agent in ("pi", "omp"):
            with self.subTest(agent=agent):
                self.assertEqual("", harness.get(agent).compaction_summary({}))


if __name__ == "__main__":
    unittest.main()
