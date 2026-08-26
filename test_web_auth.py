"""Who may drive the dashboard, in the three deployments it supports.

The dashboard starts and stops agents, deletes archives and opens pull
requests, so this is not a display concern. The invariant every mode serves:
a network bind without an identity boundary is refused.
"""

import os
import unittest
from unittest import mock

from web import auth as auth_module
from web import server


class FakeHandler:
    """Only what an auth mode is allowed to look at."""

    def __init__(self, peer="127.0.0.1", headers=None, cookie=""):
        self.client_address = (peer, 40000)
        self.headers = headers or {}
        self._cookie = cookie

    def cookies(self):
        return {"lmloop_session": self._cookie} if self._cookie else {}


class ModeSelectionTests(unittest.TestCase):
    def test_nothing_configured_is_no_auth(self):
        chosen = auth_module.build({})
        self.assertEqual("none", chosen.mode)
        self.assertFalse(chosen.trusted)
        self.assertFalse(chosen.interactive)

    def test_oidc_settings_alone_still_mean_oidc(self):
        """Every deployment that configured OIDC before the modes existed keeps
        working without being told about them."""
        with mock.patch.object(auth_module, "AVAILABLE", True):
            chosen = auth_module.build({"oidc_issuer": "https://issuer.example"})
        self.assertEqual("oidc", chosen.mode)

    def test_an_explicit_mode_beats_what_would_be_inferred(self):
        chosen = auth_module.build({
            "mode": "none", "oidc_issuer": "https://issuer.example"})
        self.assertEqual("none", chosen.mode)

    def test_an_unknown_mode_is_refused_by_name(self):
        with self.assertRaises(SystemExit) as caught:
            auth_module.build({"mode": "nonsense"})
        self.assertIn("nonsense", str(caught.exception))
        self.assertIn("none, proxy, oidc", str(caught.exception))

    def test_asking_for_oidc_without_the_libraries_is_refused(self):
        with mock.patch.object(auth_module, "AVAILABLE", False):
            with self.assertRaises(SystemExit) as caught:
                auth_module.build({"mode": "oidc"})
        self.assertIn("PyJWT", str(caught.exception))

    def test_inferred_oidc_without_the_libraries_falls_back_rather_than_refusing(self):
        """That deployment used to bind loopback and work; taking it away on an
        upgrade nobody asked for would be the wrong trade."""
        with mock.patch.object(auth_module, "AVAILABLE", False), \
             mock.patch("builtins.print"):
            chosen = auth_module.build({"oidc_issuer": "https://issuer.example"})
        self.assertEqual("none", chosen.mode)
        self.assertFalse(chosen.trusted)


class NoAuthTests(unittest.TestCase):
    def test_it_always_answers_locally(self):
        session = auth_module.NoAuth().session_for(FakeHandler())
        self.assertEqual("local", session["name"])

    def test_it_is_never_a_boundary(self):
        self.assertFalse(auth_module.NoAuth().trusted)


class ProxyAuthTests(unittest.TestCase):
    def build(self, **kwargs):
        settings = {"mode": "proxy", "trusted_proxies": ["10.0.0.5"]}
        settings.update(kwargs)
        return auth_module.build(settings)

    def test_configuring_it_without_a_trust_list_is_refused(self):
        """A header is only evidence if nothing else can set it; defaulting the
        trust list would be a way of asking attackers to name themselves."""
        with self.assertRaises(SystemExit) as caught:
            auth_module.build({"mode": "proxy"})
        self.assertIn("TRUSTED_PROXIES", str(caught.exception))

    def test_a_header_from_the_trusted_proxy_is_an_identity(self):
        session = self.build().session_for(
            FakeHandler("10.0.0.5", {"X-Forwarded-User": "alice"}))
        self.assertEqual("alice", session["user"])

    def test_the_same_header_from_anywhere_else_is_nobody(self):
        proxy = self.build()
        for peer in ("203.0.113.9", "127.0.0.1", "10.0.0.6", ""):
            with self.subTest(peer=peer):
                self.assertIsNone(proxy.session_for(
                    FakeHandler(peer, {"X-Forwarded-User": "admin"})))

    def test_the_trusted_proxy_without_a_user_is_nobody(self):
        self.assertIsNone(self.build().session_for(FakeHandler("10.0.0.5", {})))

    def test_a_blank_user_is_nobody(self):
        self.assertIsNone(self.build().session_for(
            FakeHandler("10.0.0.5", {"X-Forwarded-User": "   "})))

    def test_the_header_name_is_configurable(self):
        proxy = self.build(proxy_header="X-Auth-Request-User")
        self.assertIsNone(proxy.session_for(
            FakeHandler("10.0.0.5", {"X-Forwarded-User": "alice"})))
        self.assertEqual("alice", proxy.session_for(
            FakeHandler("10.0.0.5", {"X-Auth-Request-User": "alice"}))["user"])

    def test_a_display_name_header_is_used_when_configured(self):
        proxy = self.build(proxy_display_header="X-Forwarded-Name")
        session = proxy.session_for(FakeHandler(
            "10.0.0.5", {"X-Forwarded-User": "alice@example", "X-Forwarded-Name": "Alice"}))
        self.assertEqual("Alice", session["name"])
        self.assertEqual("alice@example", session["user"])

    def test_it_counts_as_an_identity_boundary(self):
        self.assertTrue(self.build().trusted)

    def test_it_routes_no_login_of_its_own(self):
        """The proxy already did that; offering a second one would be a lie."""
        self.assertFalse(self.build().interactive)


class NetworkBindTests(unittest.TestCase):
    """The invariant the modes exist to serve."""

    def serve_with(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return server.serve(server.configure())

    def test_an_unauthenticated_network_bind_is_refused(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.subTest(host=host):
                with self.assertRaises(SystemExit) as caught:
                    self.serve_with({"LMLOOP_WEB_HOST": host})
                message = str(caught.exception)
                self.assertIn("refusing to bind", message)
                self.assertIn("proxy", message, "say how to fix it")
                self.assertIn("oidc", message)

    def test_loopback_without_auth_is_allowed(self):
        """Nothing to refuse: it cannot be reached from off the machine."""
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                with mock.patch.dict(os.environ, {"LMLOOP_WEB_HOST": host}, clear=True), \
                     mock.patch.object(server, "ThreadingHTTPServer") as made, \
                     mock.patch("builtins.print"):
                    made.return_value.serve_forever.side_effect = KeyboardInterrupt
                    server.serve(server.configure())
                made.assert_called_once()

    def test_a_trusted_proxy_may_bind_the_network(self):
        with mock.patch.dict(os.environ, {
            "LMLOOP_WEB_HOST": "0.0.0.0",
            "LMLOOP_WEB_AUTH_MODE": "proxy",
            "LMLOOP_WEB_TRUSTED_PROXIES": "10.0.0.5",
        }, clear=True), \
             mock.patch.object(server, "ThreadingHTTPServer") as made, \
             mock.patch("builtins.print"):
            made.return_value.serve_forever.side_effect = KeyboardInterrupt
            server.serve(server.configure())
        made.assert_called_once()


class ModelListingTests(unittest.TestCase):
    """What the dashboard offers, asked of the configured agent."""

    def setUp(self):
        server._MODEL_CACHE.update(at=0.0, value=None)
        self.addCleanup(server._MODEL_CACHE.update, at=0.0, value=None)

    def config(self, **kwargs):
        settings = {"default_model": "", "harness": "pi"}
        settings.update(kwargs)
        return settings

    def test_it_asks_the_configured_agent_not_always_pi(self):
        with mock.patch.object(server.subprocess, "run") as ran:
            ran.return_value = mock.Mock(stdout="")
            server.available_models(self.config(harness="omp"), force=True)
        self.assertEqual(["omp", "models"], ran.call_args.args[0])

    def test_the_table_header_is_not_offered_as_a_model(self):
        stdout = ("provider    model    context\n"
                  "myprovider  some-model  200K\n")
        with mock.patch.object(server.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout)):
            got = server.available_models(self.config(), force=True)
        self.assertEqual(["myprovider/some-model"], got["models"])

    def test_any_provider_is_accepted_not_a_hardcoded_pair(self):
        stdout = "somebody-elses-router  a/model  1M\n"
        with mock.patch.object(server.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout)):
            got = server.available_models(self.config(), force=True)
        self.assertEqual(["somebody-elses-router/a/model"], got["models"])

    def test_an_agent_that_cannot_list_says_so(self):
        got = server.available_models(self.config(harness="opencode"), force=True)
        self.assertIn("cannot list", got["model_source"])

    def test_an_unknown_agent_does_not_crash_the_dashboard(self):
        got = server.available_models(self.config(harness="nonesuch"), force=True)
        self.assertEqual("unknown agent", got["model_source"])

    def test_nothing_is_invented_when_the_agent_cannot_be_reached(self):
        """It used to fall back to one person's model name on one person's
        server, which produces a run that dies on its first request."""
        with mock.patch.object(server.subprocess, "run", side_effect=OSError):
            got = server.available_models(self.config(), force=True)
        self.assertEqual([], got["models"])

    def test_a_configured_default_is_the_only_fallback(self):
        with mock.patch.object(server.subprocess, "run", side_effect=OSError):
            got = server.available_models(
                self.config(default_model="mine/model"), force=True)
        self.assertEqual(["mine/model"], got["models"])


if __name__ == "__main__":
    unittest.main()
