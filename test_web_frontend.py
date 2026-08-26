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

import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
APP = (ROOT / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "style.css").read_text()
RUNNER = (ROOT / "pi_runner.py").read_text()
SERVER = (ROOT / "web" / "server.py").read_text()
HTML = (ROOT / "web" / "static" / "index.html").read_text()


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


def model_sources():
    """Every answer `available_models` gives for where its list came from.

    Read from the source, like the outcomes above.  The agent's own name is not
    among these -- it is every value that is not one of these, which is what
    makes the set worth keeping in step.
    """
    found = set()
    # To the end of the line rather than to the first `}`: one of these values
    # is an f-string, and its own `{agent}` closes before the string does.
    for value in re.findall(r'"model_source":\s*(.*)', SERVER):
        found |= {literal for literal in re.findall(r'f?"([^"]*)"', value) if literal}
    return found


class ModelSourceVocabularyTests(unittest.TestCase):
    """A list the agent was never asked for renders exactly like its
    catalogue, and the difference is minutes of a run that was doomed at the
    first request.  `model_source` is the API saying which one it handed over;
    the sheet has to know every word it can say."""

    def test_the_api_reports_the_sources_this_test_thinks_it_does(self):
        """A guard on the guard: finding nothing would pass everything."""
        sources = model_sources()
        self.assertGreaterEqual(len(sources), 4, sources)
        self.assertIn("fallback", sources)
        self.assertIn("{agent} cannot list", sources)

    def test_the_sheet_can_explain_every_list_that_is_not_a_catalogue(self):
        block = re.search(r"const MODEL_SOURCE_REASON = \{(.*?)\n\};", APP, re.S)
        self.assertIsNotNone(block, "MODEL_SOURCE_REASON moved or was renamed")
        known = set(re.findall(r'"([^"]+)":', block.group(1)))
        for source in model_sources():
            with self.subTest(source=source):
                if source.startswith("{agent} "):
                    # This one carries the agent's own name, so the sheet
                    # matches it by suffix rather than by lookup.
                    self.assertIn(source[len("{agent} "):], APP)
                else:
                    self.assertIn(source, known)

    def test_the_reason_reaches_a_paragraph_that_exists(self):
        """The whole feature is one element; a renamed id makes it silent."""
        self.assertIn('id="model-source"', HTML)
        self.assertIn('$("model-source").textContent = source', APP)
        self.assertIn('$("model-source").hidden = !source', APP)

    def test_a_real_catalogue_says_nothing(self):
        """`model_source` is the agent's name in the normal case, and the
        normal case was promised nothing new on screen."""
        body = re.search(r"function modelSourceNote\(catalogue\) \{(.*?)\n\}", APP, re.S)
        self.assertIsNotNone(body, "modelSourceNote moved or was renamed")
        self.assertIn('if (!reason) return "";', body.group(1))


class RunFieldContractTests(unittest.TestCase):
    """Every `run.<field>` the dashboard reads is one the API really serves.

    The other half of the drift this file exists for. `OUTCOME_CLASS` catches a
    word the dashboard does not know; this catches a field the dashboard thinks
    it knows and the API never sends -- which renders as nothing at all, and so
    reads as "this run has none of that" rather than as a bug.
    """

    def payload_keys(self):
        """The keys `web/runs.py` really returns, from really calling it."""
        import eta
        from web import runs as runs_module

        base = Path(tempfile.mkdtemp())
        run_dir = base / ".worktrees" / "r" / ".lmloop" / "runs" / "r"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({
            "phase": "stopped", "model": "local/model", "updated_at": "",
        }))
        (run_dir / "lmloop.log").write_text(
            json.dumps({"event": "run:start", "agent": "omp"}) + "\n"
        )
        served = set(runs_module.detail({"id": "p", "path": str(base)}, run_dir))
        # The estimate is spread into the summary and only for a running run,
        # so its keys are absent above.  Read them from `eta` rather than
        # listing them here, for the same reason as everything else in this
        # file.
        served |= set(eta.estimate(
            [{"event": "iteration:end", "outcome": "ok", "elapsedMs": 1000},
             {"event": "iteration:end", "outcome": "ok", "elapsedMs": 1000}],
            iteration=1, max_iterations=4,
        ))
        return served

    def test_the_api_fixture_this_test_relies_on_still_produces_a_payload(self):
        """A guard on the guard: an empty payload would pass everything."""
        served = self.payload_keys()
        self.assertGreaterEqual(len(served), 30, served)
        for expected in ("state", "agent", "model", "eta_seconds"):
            self.assertIn(expected, served)

    def test_every_run_field_the_dashboard_reads_is_one_the_api_sends(self):
        read = set(re.findall(r"\brun\.([a-z_]+)\b", APP))
        missing = read - self.payload_keys()
        self.assertEqual(set(), missing,
                         f"fields the dashboard reads and the API never sends: {sorted(missing)}")

class AgentAttributionTests(unittest.TestCase):
    """lmloop drives pi, omp and opencode, and they fail differently enough
    that reading a failure starts with knowing which one produced it.  The run
    record names it because the `run:start` event used to say "pi" whatever it
    was; the dashboard has to say it for that to be worth anything.
    """

    def function_body(self, name):
        match = re.search(rf"function {name}\(.*?\) \{{(.*?)\n\}}", APP, re.S)
        self.assertIsNotNone(match, f"{name} moved or was renamed")
        return match.group(1)

    def test_the_run_card_names_the_agent(self):
        self.assertIn("run.agent", self.function_body("metaBits"))

    def test_the_run_view_names_the_agent(self):
        self.assertIn("run.agent", self.function_body("patchModel"))

    def test_a_run_from_before_the_field_existed_shows_no_empty_separator(self):
        """Six archived runs predate `agent`, and every run archived by an
        older lmloop always will.  Both surfaces join with a separator, so an
        empty value is not merely invisible -- it is a stray dot."""
        self.assertIn("if (run.agent) bits.push(run.agent)", self.function_body("metaBits"))
        self.assertIn(".filter(Boolean)", self.function_body("patchModel"))

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
