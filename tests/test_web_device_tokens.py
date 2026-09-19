"""Device tokens: read-only API access for a client with no cookie jar.

The invariant under test is the one `web/device_auth.py`'s module docstring
states: a device token can read `/api/*` but can never mutate a run, because
`do_POST` never consults it -- not because a flag forbids it.
"""

import http.client
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from http.server import ThreadingHTTPServer

from web import device_auth
from web import server


class DeviceTokensTests(unittest.TestCase):
    def build(self, **labels):
        return device_auth.DeviceTokens(dict(labels))

    def test_a_configured_token_matches_its_own_bearer_header(self):
        tokens = self.build(phone="secret-value")
        self.assertEqual("phone", tokens.match("Bearer secret-value"))

    def test_a_wrong_token_matches_nothing(self):
        tokens = self.build(phone="secret-value")
        self.assertIsNone(tokens.match("Bearer not-the-token"))

    def test_a_missing_header_matches_nothing(self):
        self.assertIsNone(self.build(phone="x").match(""))

    def test_a_header_without_the_bearer_scheme_matches_nothing(self):
        """A cookie or a raw token pasted into `Authorization` is not a
        bearer credential just because a device token happens to equal it."""
        tokens = self.build(phone="secret-value")
        self.assertIsNone(tokens.match("secret-value"))
        self.assertIsNone(tokens.match("Basic secret-value"))

    def test_an_empty_bearer_value_matches_nothing(self):
        self.assertIsNone(self.build(phone="x").match("Bearer "))

    def test_multiple_devices_each_have_their_own_token(self):
        tokens = self.build(phone="p-token", tablet="t-token")
        self.assertEqual("phone", tokens.match("Bearer p-token"))
        self.assertEqual("tablet", tokens.match("Bearer t-token"))

    def test_no_tokens_configured_is_falsy(self):
        self.assertFalse(self.build())
        self.assertTrue(self.build(phone="x"))


class BuildFromEnvStringTests(unittest.TestCase):
    def test_a_literal_reference_is_used_as_is(self):
        tokens = device_auth.build("phone=hunter2")
        self.assertEqual("phone", tokens.match("Bearer hunter2"))

    def test_an_env_reference_is_resolved(self):
        with mock.patch.dict("os.environ", {"MY_DEVICE_TOKEN": "from-env"}, clear=False):
            tokens = device_auth.build("phone=env:MY_DEVICE_TOKEN")
        self.assertEqual("phone", tokens.match("Bearer from-env"))

    def test_a_reference_that_may_itself_contain_equals_signs_survives(self):
        """`!command` and some literal values can legally contain `=`; only
        the first `=` in an entry separates the label from the reference."""
        tokens = device_auth.build("phone=a=b=c")
        self.assertEqual("phone", tokens.match("Bearer a=b=c"))

    def test_a_malformed_entry_with_no_equals_sign_is_skipped_not_fatal(self):
        with mock.patch("builtins.print"):
            tokens = device_auth.build("phone=good-token,justsomejunk")
        self.assertEqual("phone", tokens.match("Bearer good-token"))

    def test_a_reference_that_resolves_to_nothing_denies_that_device_only(self):
        """One bad device token should deny one device, not the whole
        dashboard."""
        with mock.patch("builtins.print"):
            tokens = device_auth.build("phone=env:DEFINITELY_NOT_SET_ANYWHERE,tablet=good-token")
        self.assertIsNone(tokens.match("Bearer "))
        self.assertEqual("tablet", tokens.match("Bearer good-token"))

    def test_an_empty_string_configures_nothing(self):
        self.assertFalse(device_auth.build(""))

    def test_whitespace_around_entries_and_labels_is_trimmed(self):
        tokens = device_auth.build(" phone = good-token , tablet=other ")
        self.assertEqual("phone", tokens.match("Bearer good-token"))
        self.assertEqual("tablet", tokens.match("Bearer other"))


class FakeHandler:
    """Only what `Handler.device_session` is allowed to look at."""

    def __init__(self, device_tokens, authorization=""):
        self.device_tokens = device_tokens
        self.headers = {"Authorization": authorization} if authorization else {}


class RejectAuth:
    mode = "proxy"
    interactive = False
    enabled = True
    trusted = True

    def session_for(self, _handler):
        return None


class HTTPBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.server.daemon_threads = True
        server.Handler.config = {
            "roots": [Path(self.root.name)],
            "read_only": False,
            "default_model": "",
            "default_max_iterations": 20,
            "default_thinking": "low",
            "poll_seconds": 3,
            "hidden_poll_seconds": 30,
        }
        server.Handler.auth = RejectAuth()
        server.Handler.device_tokens = device_auth.DeviceTokens({"phone": "secret"})
        server.Handler.push = SimpleNamespace(public_key="", enabled=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.root.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address)
        encoded = None if body is None else body.encode()
        connection.request(method, path, encoded, headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_valid_device_token_gets_api_config_over_http(self):
        status, body = self.request("GET", "/api/config", {"Authorization": "Bearer secret"})
        self.assertEqual(200, status)
        self.assertEqual("device:phone", json.loads(body)["user"])
        self.assertEqual("disabled", json.loads(body)["csrf"])

    def test_invalid_device_token_is_denied_over_http(self):
        status, _body = self.request("GET", "/api/config", {"Authorization": "Bearer wrong"})
        self.assertEqual(401, status)

    def test_device_token_cannot_mutate_over_http(self):
        status, _body = self.request(
            "POST", "/api/runs", {"Authorization": "Bearer secret", "Content-Length": "2"}, "{}")
        self.assertEqual(401, status)

    def test_interactive_mutation_requires_csrf_over_http(self):
        class InteractiveAuth:
            mode = "oidc"
            interactive = True
            enabled = True
            trusted = True

            def session_for(self, handler):
                if handler.cookies().get("lmloop_session") == "cookie":
                    return {"name": "alice", "csrf": "csrf-value"}
                return None

        server.Handler.auth = InteractiveAuth()
        headers = {"Cookie": "lmloop_session=cookie", "Content-Length": "2"}
        for csrf in (None, "wrong"):
            with self.subTest(csrf=csrf):
                request_headers = dict(headers)
                if csrf is not None:
                    request_headers["X-CSRF-Token"] = csrf
                status, _body = self.request("POST", "/api/runs", request_headers, "{}")
                self.assertEqual(403, status)

    def test_interactive_mutation_with_csrf_reaches_operation(self):
        class InteractiveAuth:
            mode = "oidc"
            interactive = True
            enabled = True
            trusted = True

            def session_for(self, handler):
                return {"name": "alice", "csrf": "csrf-value"} if handler.cookies().get("lmloop_session") else None

        server.Handler.auth = InteractiveAuth()
        with mock.patch.object(server.service, "start_run", return_value=(400, {"error": "test"})) as start:
            status, _body = self.request(
                "POST", "/api/runs",
                {"Cookie": "lmloop_session=cookie", "X-CSRF-Token": "csrf-value", "Content-Length": "2"},
                "{}",
            )
        self.assertEqual(400, status)
        start.assert_called_once()


class DeviceSessionTests(unittest.TestCase):
    def test_a_valid_token_returns_a_session_shaped_like_a_real_one(self):
        tokens = device_auth.DeviceTokens({"phone": "secret"})
        session = server.Handler.device_session(FakeHandler(tokens, "Bearer secret"))
        self.assertEqual("device:phone", session["name"])
        self.assertIn("csrf", session)

    def test_no_device_tokens_configured_is_nobody(self):
        session = server.Handler.device_session(FakeHandler(None))
        self.assertIsNone(session)

    def test_an_invalid_token_is_nobody(self):
        tokens = device_auth.DeviceTokens({"phone": "secret"})
        session = server.Handler.device_session(FakeHandler(tokens, "Bearer wrong"))
        self.assertIsNone(session)


class ReadOnlyByConstructionTests(unittest.TestCase):
    """The invariant this module exists to serve: however it is configured, a
    device token cannot mutate a run. Proven structurally -- `do_POST` must
    never call `device_session`, the same way `do_POST` is source-read
    elsewhere in this project's tests to prove what it does and does not do
    (see CLAUDE.md on the destructive-action test)."""

    def test_do_post_never_consults_device_session(self):
        source = inspect.getsource(server.Handler.do_POST)
        self.assertNotIn("device_session", source)

    def test_do_post_only_ever_authenticates_via_require_auth(self):
        source = inspect.getsource(server.Handler.do_POST)
        self.assertIn("self.require_auth(api=True)", source)


if __name__ == "__main__":
    unittest.main()
