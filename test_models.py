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

    def test_a_router_model_falls_back_to_the_harness_config(self):
        with self.with_provider("llama-swap"), \
             mock.patch.object(models, "_pi_declared", return_value=(200000, 50000)) as declared:
            self.assertEqual((200000, 50000), models.declared_window("openrouter/x"))
        declared.assert_called_once_with("openrouter", "x")

    def test_with_no_local_provider_even_a_llama_swap_id_takes_the_router_path(self):
        """Turning the local path off has to turn it off for everything, or a
        machine with no local server still ends up reading a context cache."""
        with self.with_provider(""), \
             mock.patch.object(models, "_pi_declared", return_value=(1000, 100)) as declared, \
             mock.patch.object(models, "load_cache") as cache:
            self.assertEqual((1000, 100), models.declared_window("llama-swap/local-fast"))
        declared.assert_called_once_with("llama-swap", "local-fast")
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


if __name__ == "__main__":
    unittest.main()
