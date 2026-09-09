import os
import unittest
from unittest import mock

import env


class LooksSecretTests(unittest.TestCase):
    def test_ordinary_names_are_not_secrets(self):
        for name in ("PATH", "HOME", "LANG", "NODE_OPTIONS", "CARGO_HOME", "TERM"):
            self.assertFalse(env.looks_secret(name), name)

    def test_credential_shaped_names_are(self):
        for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY",
                     "NPM_PASSWORD", "SSH_AUTH_SOCK", "MY_PRIVATE_KEY",
                     "SESSION_ID", "HTTP_COOKIE"):
            self.assertTrue(env.looks_secret(name), name)

    def test_the_check_is_case_insensitive(self):
        """`npm_config__auth` is a registry password spelled in lower case."""
        self.assertTrue(env.looks_secret("npm_config__auth"))
        self.assertTrue(env.looks_secret("github_token"))


class MatchesTests(unittest.TestCase):
    def test_an_exact_name_matches_only_itself(self):
        self.assertTrue(env.matches("PATH", ("PATH",)))
        self.assertFalse(env.matches("PATHEXT", ("PATH",)))

    def test_a_trailing_star_is_a_prefix(self):
        self.assertTrue(env.matches("PI_CODING_AGENT_DIR", ("PI_*",)))
        self.assertTrue(env.matches("PI_", ("PI_*",)))
        self.assertFalse(env.matches("PICKLE", ("PI_*",)))


class BuildTests(unittest.TestCase):
    HOST = {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "LC_ALL": "C.UTF-8",
        "NODE_OPTIONS": "--max-old-space-size=4096",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "GITHUB_TOKEN": "leak",
        "NODE_AUTH_TOKEN": "leak",
        "DATABASE_URL": "postgres://user:pw@host/db",
        "PI_CODING_AGENT_DIR": "/scratch/pi",
        "OMP_THREADS": "8",
        "SOMEONES_UNRELATED_VAR": "x",
    }

    def build(self, **kwargs):
        return env.build(self.HOST, **kwargs)

    def test_the_basics_a_process_needs_survive(self):
        kept = self.build()
        for name in ("PATH", "HOME", "LC_ALL", "NODE_OPTIONS"):
            self.assertIn(name, kept)

    def test_unrelated_host_credentials_are_absent_by_default(self):
        kept = self.build()
        for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(name, kept)

    def test_a_variable_nobody_allowed_is_absent_even_though_it_is_harmless(self):
        """The list is an allowlist, not a denylist: not being a credential is
        not a reason to be inherited."""
        self.assertNotIn("SOMEONES_UNRELATED_VAR", self.build())
        self.assertNotIn("DATABASE_URL", self.build())

    def test_a_prefix_rule_cannot_let_a_credential_through(self):
        """`NODE_*` is a reasonable thing to allow and `NODE_AUTH_TOKEN` is a
        registry credential.  The name filter is what separates them."""
        kept = self.build()
        self.assertIn("NODE_OPTIONS", kept)
        self.assertNotIn("NODE_AUTH_TOKEN", kept)

    def test_a_harness_gets_its_own_namespace(self):
        kept = self.build(harness_names=("PI_*",))
        self.assertIn("PI_CODING_AGENT_DIR", kept)
        self.assertNotIn("OMP_THREADS", kept)

    def test_a_second_harness_namespace_is_additive(self):
        kept = self.build(harness_names=("PI_*", "OMP_*"))
        self.assertIn("PI_CODING_AGENT_DIR", kept)
        self.assertIn("OMP_THREADS", kept)

    def test_naming_a_credential_explicitly_opts_it_in(self):
        """The one list somebody typed on purpose beats the heuristic."""
        kept = self.build(allow=("GITHUB_TOKEN",))
        self.assertEqual("leak", kept["GITHUB_TOKEN"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kept)

    def test_an_opt_in_prefix_does_not_exempt_credentials_it_did_not_name(self):
        """A prefix in `pass` still adds names, but exempting a credential
        takes naming it: `AWS_*` should not quietly hand over the secret key."""
        kept = self.build(allow=("AWS_*",))
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kept)

    def test_block_wins_over_everything(self):
        kept = self.build(allow=("GITHUB_TOKEN",), block=("GITHUB_TOKEN", "PATH"))
        self.assertNotIn("GITHUB_TOKEN", kept)
        self.assertNotIn("PATH", kept)

    def test_block_accepts_a_prefix_too(self):
        kept = self.build(harness_names=("PI_*",), block=("PI_*",))
        self.assertNotIn("PI_CODING_AGENT_DIR", kept)

    def test_inherit_all_is_the_old_behaviour(self):
        kept = self.build(inherit="all")
        self.assertEqual(set(self.HOST), set(kept))

    def test_inherit_all_still_honours_block(self):
        kept = self.build(inherit="all", block=("AWS_*",))
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kept)
        self.assertIn("GITHUB_TOKEN", kept)

    def test_overrides_are_the_loops_own_and_are_never_filtered(self):
        kept = self.build(overrides={"PYTHONPYCACHEPREFIX": "/run/pycache"})
        self.assertEqual("/run/pycache", kept["PYTHONPYCACHEPREFIX"])

    def test_an_override_beats_the_host_value(self):
        kept = env.build({"PATH": "/host"}, overrides={"PATH": "/ours"})
        self.assertEqual("/ours", kept["PATH"])

    def test_nothing_is_invented(self):
        """Every name out came from the host or the overrides."""
        kept = self.build(harness_names=("PI_*",), allow=("GITHUB_TOKEN",))
        for name in kept:
            self.assertIn(name, self.HOST)


class WithheldTests(unittest.TestCase):
    def test_it_names_credentials_the_child_will_not_see(self):
        host = {"PATH": "/bin", "GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y"}
        passed = {"PATH": "/bin"}
        self.assertEqual(
            ["AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"], env.withheld(host, passed),
        )

    def test_a_credential_that_was_passed_is_not_reported(self):
        host = {"GITHUB_TOKEN": "x"}
        self.assertEqual([], env.withheld(host, {"GITHUB_TOKEN": "x"}))

    def test_ordinary_dropped_variables_are_not_reported(self):
        """Only the ones somebody might be missing, not every filtered name."""
        host = {"SOMEONES_UNRELATED_VAR": "x"}
        self.assertEqual([], env.withheld(host, {}))


class RedactTests(unittest.TestCase):
    def test_credential_values_are_masked(self):
        masked = env.redact({"PATH": "/bin", "GITHUB_TOKEN": "ghp_realsecret"})
        self.assertEqual("/bin", masked["PATH"])
        self.assertEqual(env.REDACTED, masked["GITHUB_TOKEN"])

    def test_the_names_survive_so_the_shape_is_still_readable(self):
        masked = env.redact({"AWS_SECRET_ACCESS_KEY": "x"})
        self.assertIn("AWS_SECRET_ACCESS_KEY", masked)

    def test_the_secret_value_appears_nowhere_in_the_result(self):
        masked = env.redact({"GITHUB_TOKEN": "ghp_realsecret"})
        self.assertNotIn("ghp_realsecret", str(masked))


if __name__ == "__main__":
    unittest.main()
