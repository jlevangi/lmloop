"""The dashboard's vocabulary, kept in step with the runner's.

The frontend is served as static files, so nothing imports it and nothing type
checks it against the API it renders. That makes one kind of drift invisible:
the runner grows an outcome, the dashboard does not know the word, and a
failure renders as neutral -- which is worse than not rendering at all,
because it reads as "fine".

That is not hypothetical. `tool-timeout` was added to `pi_runner` in this
project's own history and appeared in the dashboard as an unstyled cell and a
grey pip until this test was written.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
APP = (ROOT / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "style.css").read_text()
RUNNER = (ROOT / "pi_runner.py").read_text()


def runner_outcomes():
    """Every outcome `pi_runner` can hand back, read from the source.

    Read rather than listed, so a new one is picked up by the test that is
    supposed to notice new ones.
    """
    found = set(re.findall(r'outcome, detail = "([a-z-]+)"', RUNNER))
    found |= set(re.findall(r'outcome = "([a-z-]+)"', RUNNER))
    return found


class OutcomeVocabularyTests(unittest.TestCase):
    def test_the_runner_produces_the_outcomes_this_test_thinks_it_does(self):
        """A guard on the guard: if the source stops matching these shapes,
        everything below would pass by finding nothing."""
        outcomes = runner_outcomes()
        self.assertGreaterEqual(len(outcomes), 8, outcomes)
        for expected in ("ok", "timeout", "tool-timeout", "agent-error"):
            self.assertIn(expected, outcomes)

    def test_every_outcome_has_a_class_in_the_dashboard(self):
        block = re.search(r"const OUTCOME_CLASS = \{(.*?)\};", APP, re.S)
        self.assertIsNotNone(block, "OUTCOME_CLASS moved or was renamed")
        known = set(re.findall(r'"?([a-z-]+)"?\s*:', block.group(1)))
        missing = runner_outcomes() - known
        self.assertEqual(set(), missing,
                         f"outcomes the dashboard would render unstyled: {sorted(missing)}")

    def test_every_failing_outcome_is_visibly_a_failure(self):
        """Neutral is worse than missing: it reads as fine."""
        block = re.search(r"const OUTCOME_CLASS = \{(.*?)\};", APP, re.S).group(1)
        classes = dict(re.findall(r'"?([a-z-]+)"?\s*:\s*"([a-z]*)"', block))
        for outcome in ("stalled", "timeout", "tool-timeout", "agent-error"):
            with self.subTest(outcome=outcome):
                self.assertEqual("bad", classes.get(outcome))
        for outcome in ("thrashing", "truncated", "no-action"):
            with self.subTest(outcome=outcome):
                self.assertEqual("warn", classes.get(outcome))

    def test_every_failing_outcome_also_colours_its_pip(self):
        """The pips are the only view of a run's history on a phone, and they
        are styled by CSS rather than by the class map above."""
        rules = [line for line in CSS.splitlines() if line.startswith(".pip")]
        bad = " ".join(line for line in rules if "--bad" in line)
        warn = " ".join(line for line in rules if "--warn" in line)
        for outcome, expected in (("stalled", bad), ("timeout", bad),
                                  ("tool-timeout", bad), ("agent-error", bad),
                                  ("thrashing", warn), ("truncated", warn),
                                  ("no-action", warn)):
            with self.subTest(outcome=outcome):
                # Either an exact class, or a substring selector naming the
                # whole outcome or one of its words -- the stylesheet uses all
                # three shapes, and which one is a formatting choice.
                candidates = [outcome, *outcome.split("-")]
                matched = (
                    f".pip.{outcome}" in expected
                    or any(f'[class*="{fragment}"]' in expected
                           for fragment in candidates)
                )
                self.assertTrue(matched, f"{outcome} has no colour among: {expected}")


class RunStateVocabularyTests(unittest.TestCase):
    def test_every_state_the_api_computes_is_one_the_dashboard_knows(self):
        served = set(re.findall(r'return \("?([a-z]+)"', (ROOT / "web" / "runs.py").read_text()))
        served |= set(re.findall(r'return "([a-z]+)", age', (ROOT / "web" / "runs.py").read_text()))
        served -= {"unknown"}
        for state in served:
            with self.subTest(state=state):
                self.assertIn(state, APP, f"the dashboard never mentions state {state!r}")


if __name__ == "__main__":
    unittest.main()
