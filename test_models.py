import unittest
from unittest import mock

import models


class ProviderOfTests(unittest.TestCase):
    def test_the_prefix_before_the_first_slash_is_the_provider(self):
        self.assertEqual("llama-swap", models.provider_of("llama-swap/local-fast"))
        self.assertEqual("openrouter", models.provider_of("openrouter/some/nested/id"))

    def test_a_bare_name_has_no_provider(self):
        self.assertEqual("", models.provider_of("local-fast"))


class LocalProviderTests(unittest.TestCase):
    """Which provider means "a local server this machine can measure itself".

    Named in the budget policy rather than hardcoded: llama-swap is one
    deployment, not a requirement, and a machine without one has to be able to
    turn the whole local path off.
    """

    def with_provider(self, name):
        policy = dict(models._FALLBACK, local_provider=name)
        return mock.patch.object(models, "budgets", return_value=policy)

    def test_the_default_is_llama_swap(self):
        self.assertEqual("llama-swap", models._FALLBACK["local_provider"])

    def test_a_model_from_the_local_provider_is_local(self):
        with self.with_provider("llama-swap"):
            self.assertTrue(models.is_local("llama-swap/local-fast"))

    def test_a_router_model_is_not(self):
        with self.with_provider("llama-swap"):
            self.assertFalse(models.is_local("openrouter/anthropic/claude"))

    def test_nothing_is_local_when_no_local_provider_is_configured(self):
        """The supported configuration for a machine with no local server."""
        with self.with_provider(""):
            self.assertFalse(models.is_local("llama-swap/local-fast"))
            self.assertFalse(models.is_local("openrouter/anything"))

    def test_the_local_provider_can_be_something_else_entirely(self):
        with self.with_provider("my-own-server"):
            self.assertTrue(models.is_local("my-own-server/whatever"))
            self.assertFalse(models.is_local("llama-swap/local-fast"))

    def test_a_bare_model_name_is_never_local(self):
        with self.with_provider("llama-swap"):
            self.assertFalse(models.is_local("local-fast"))


class PreflightTests(unittest.TestCase):
    def with_provider(self, name):
        policy = dict(models._FALLBACK, local_provider=name)
        return mock.patch.object(models, "budgets", return_value=policy)

    def test_a_router_model_is_not_preflighted(self):
        """There is nothing cheap to check, so nothing is checked -- and a
        dead router surfaces as an agent-error iteration, which still commits."""
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "running") as running:
            ok, detail = models.preflight("openrouter/x", "http://127.0.0.1:8080")
        self.assertTrue(ok)
        self.assertIn("non-local", detail)
        running.assert_not_called()

    def test_nothing_is_preflighted_when_there_is_no_local_provider(self):
        with self.with_provider(""), mock.patch.object(models, "running") as running:
            ok, _ = models.preflight("llama-swap/local-fast", "http://127.0.0.1:8080")
        self.assertTrue(ok)
        running.assert_not_called()

    def test_an_already_loaded_model_says_so(self):
        entries = [{"model": "local-fast", "state": "ready"}]
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "running", return_value=entries):
            ok, detail = models.preflight("llama-swap/local-fast", "http://x")
        self.assertTrue(ok)
        self.assertIn("already loaded", detail)

    def test_a_different_model_being_loaded_is_not_an_error(self):
        """The first request will evict and load, which costs minutes -- which
        is why the stall timer does not start until the first event."""
        entries = [{"model": "local-wide", "state": "ready"}]
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "running", return_value=entries):
            ok, detail = models.preflight("llama-swap/local-fast", "http://x")
        self.assertTrue(ok)
        self.assertIn("will swap", detail)

    def test_an_unreachable_server_is_the_one_real_failure(self):
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "running", side_effect=OSError("refused")):
            ok, detail = models.preflight("llama-swap/local-fast", "http://x")
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class DeclaredWindowTests(unittest.TestCase):
    def with_provider(self, name):
        policy = dict(models._FALLBACK, local_provider=name)
        return mock.patch.object(models, "budgets", return_value=policy)

    def test_a_model_with_no_provider_is_unknown(self):
        self.assertIsNone(models.declared_window("bare-name"))

    def test_a_local_model_comes_from_the_measured_cache(self):
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "load_cache", return_value={"local-fast": 65536}):
            window = models.declared_window("llama-swap/local-fast")
        self.assertIsNotNone(window)
        context, output = window
        # real 65536 minus the local-fast output override of 16384.
        self.assertEqual(65536 - 16384, context)
        self.assertEqual(16384, output)

    def test_an_unmeasured_local_model_is_unknown_rather_than_guessed(self):
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "load_cache", return_value={}):
            self.assertIsNone(models.declared_window("llama-swap/never-measured"))

    def test_a_router_model_comes_from_the_agents_own_catalogue(self):
        catalogue = {"openrouter/x": (200000, 50000)}
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "harness_windows", return_value=catalogue) as asked:
            self.assertEqual((200000, 50000), models.declared_window("openrouter/x", "omp"))
        asked.assert_called_once_with("omp")

    def test_a_model_the_agent_has_never_heard_of_is_unknown(self):
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "harness_windows", return_value={}):
            self.assertIsNone(models.declared_window("openrouter/mystery", "pi"))

    def test_with_no_local_provider_even_a_llama_swap_id_takes_the_router_path(self):
        """Turning the local path off has to turn it off for everything, or a
        machine with no local server still ends up reading a context cache."""
        catalogue = {"llama-swap/local-fast": (1000, 100)}
        with self.with_provider(""), \
             mock.patch.object(models, "harness_windows", return_value=catalogue), \
             mock.patch.object(models, "load_cache") as cache:
            self.assertEqual((1000, 100), models.declared_window("llama-swap/local-fast"))
        cache.assert_not_called()


class BudgetsTests(unittest.TestCase):
    def test_a_missing_file_leaves_the_defaults_intact(self):
        with mock.patch.object(models.Path, "read_text", side_effect=OSError):
            self.assertEqual(models._FALLBACK, models.budgets())

    def test_prose_keys_are_ignored(self):
        payload = '{"_note": "for humans", "headroom": 4096}'
        with mock.patch.object(models.Path, "read_text", return_value=payload):
            policy = models.budgets()
        self.assertEqual(4096, policy["headroom"])
        self.assertNotIn("_note", policy)

    def test_an_unparseable_file_changes_nothing(self):
        with mock.patch.object(models.Path, "read_text", return_value="{not json"):
            self.assertEqual(models._FALLBACK, models.budgets())


class HarnessWindowsCacheTests(unittest.TestCase):
    """The agent's catalogue is fetched at most once per process.

    Not an optimisation: `omp models --json` takes about two seconds, and
    `Run._wider_model` walks every candidate model looking for a wider window.
    """

    def setUp(self):
        models.forget_harness_windows()
        self.addCleanup(models.forget_harness_windows)

    def fake_adapter(self, windows):
        return mock.Mock(declared_windows=mock.Mock(return_value=windows))

    def test_a_catalogue_is_fetched_once_however_often_it_is_asked(self):
        adapter = self.fake_adapter({"p/m": (100, 10)})
        with mock.patch("harness.get", return_value=adapter):
            for _ in range(5):
                self.assertEqual({"p/m": (100, 10)}, models.harness_windows("omp"))
        adapter.declared_windows.assert_called_once()

    def test_each_agent_is_cached_separately(self):
        pi = self.fake_adapter({"p/pi-only": (1, 1)})
        omp = self.fake_adapter({"p/omp-only": (2, 2)})
        with mock.patch("harness.get", side_effect=lambda n: pi if n == "pi" else omp):
            self.assertIn("p/pi-only", models.harness_windows("pi"))
            self.assertIn("p/omp-only", models.harness_windows("omp"))
            self.assertNotIn("p/omp-only", models.harness_windows("pi"))

    def test_an_unknown_agent_is_an_empty_catalogue_not_a_crash(self):
        with mock.patch("harness.get", side_effect=SystemExit("unknown harness")):
            self.assertEqual({}, models.harness_windows("nonesuch"))

    def test_an_unknown_agent_is_not_retried_either(self):
        with mock.patch("harness.get", side_effect=SystemExit("unknown")) as get:
            models.harness_windows("nonesuch")
            models.harness_windows("nonesuch")
        get.assert_called_once()

    def test_forgetting_makes_the_next_call_fetch_again(self):
        adapter = self.fake_adapter({})
        with mock.patch("harness.get", return_value=adapter):
            models.harness_windows("pi")
            models.forget_harness_windows()
            models.harness_windows("pi")
        self.assertEqual(2, adapter.declared_windows.call_count)


if __name__ == "__main__":
    unittest.main()
