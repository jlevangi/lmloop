"""Web Push (VAPID): key handling, subscription storage, and the notification
text it shares with ntfy rather than re-deriving.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notify
import webpush
from web import push as push_module
from web import push_store


def generate_pem() -> str:
    from py_vapid import Vapid01
    vapid = Vapid01()
    vapid.generate_keys()
    return vapid.private_pem().decode()


class WebPushConfigTests(unittest.TestCase):
    def test_disabled_without_a_library(self):
        with mock.patch.object(push_module, "AVAILABLE", False):
            push = push_module.build(generate_pem(), "mailto:you@example.com")
        self.assertFalse(push.enabled)
        self.assertEqual("", push.public_key)

    def test_disabled_without_a_private_key(self):
        push = push_module.build("", "mailto:you@example.com")
        self.assertFalse(push.enabled)

    def test_disabled_without_a_contact(self):
        push = push_module.build(generate_pem(), "")
        self.assertFalse(push.enabled)

    def test_a_malformed_key_disables_push_rather_than_raising(self):
        with mock.patch("builtins.print"):
            push = push_module.build("not a real pem", "mailto:you@example.com")
        self.assertFalse(push.enabled)

    def test_enabled_with_a_real_key_and_contact(self):
        push = push_module.build(generate_pem(), "mailto:you@example.com")
        self.assertTrue(push.enabled)

    def test_the_public_key_is_a_url_safe_base64_uncompressed_point(self):
        """This is exactly the shape `PushManager.subscribe`'s
        `applicationServerKey` needs: a 65-byte uncompressed P-256 point,
        base64url-encoded -- 87 characters starting with `B` (the encoding
        of the 0x04 uncompressed-point prefix byte)."""
        push = push_module.build(generate_pem(), "mailto:you@example.com")
        self.assertEqual(87, len(push.public_key))
        self.assertTrue(push.public_key.startswith("B"))
        self.assertNotIn("+", push.public_key)
        self.assertNotIn("/", push.public_key)

    def test_a_deployment_that_never_configured_push_reports_no_public_key(self):
        push = push_module.build("", "")
        self.assertEqual("", push.public_key)


class PushStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "push_subscriptions.json"

    def test_adding_a_subscription_makes_it_appear_in_the_list(self):
        push_store.add(self.path, {"endpoint": "https://push.example/1", "keys": {}})
        self.assertEqual(1, len(push_store.all_subscriptions(self.path)))

    def test_re_subscribing_the_same_endpoint_replaces_rather_than_duplicates(self):
        push_store.add(self.path, {"endpoint": "https://push.example/1", "keys": {"auth": "old"}})
        push_store.add(self.path, {"endpoint": "https://push.example/1", "keys": {"auth": "new"}})
        subscriptions = push_store.all_subscriptions(self.path)
        self.assertEqual(1, len(subscriptions))
        self.assertEqual("new", subscriptions[0]["keys"]["auth"])

    def test_different_endpoints_both_persist(self):
        push_store.add(self.path, {"endpoint": "https://push.example/1"})
        push_store.add(self.path, {"endpoint": "https://push.example/2"})
        self.assertEqual(2, len(push_store.all_subscriptions(self.path)))

    def test_removing_by_endpoint_drops_only_that_one(self):
        push_store.add(self.path, {"endpoint": "https://push.example/1"})
        push_store.add(self.path, {"endpoint": "https://push.example/2"})
        push_store.remove(self.path, "https://push.example/1")
        remaining = push_store.all_subscriptions(self.path)
        self.assertEqual(["https://push.example/2"], [s["endpoint"] for s in remaining])

    def test_a_subscription_with_no_endpoint_is_not_stored(self):
        push_store.add(self.path, {"keys": {}})
        self.assertEqual([], push_store.all_subscriptions(self.path))

    def test_reading_a_store_that_does_not_exist_yet_is_an_empty_list(self):
        self.assertEqual([], push_store.all_subscriptions(self.path))

    def test_writes_are_atomic(self):
        """No `.json.tmp` left behind, and the real file is always valid
        JSON -- the same guarantee `rundir.write_status` makes for
        `status.json`, extended here to the first web-side file more than
        one request thread can write at once."""
        push_store.add(self.path, {"endpoint": "https://push.example/1"})
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())
        json.loads(self.path.read_text())  # does not raise


class WebPushSendTests(unittest.TestCase):
    """`webpush.send` -- the piece `loop.py` calls -- reuses `notify.summarise`
    rather than re-deriving title/body, and is a safe no-op whenever there is
    nothing to do."""

    def run_dict(self, **overrides):
        base = {"repo": "myapp", "commits": 3, "iterations": 5, "hours": 1.2,
                "plan": (2, 4), "reason": "complete", "failures": {},
                "objective": "do the thing", "project": "myapp", "run_id": "r1"}
        base.update(overrides)
        return base

    def test_no_vapid_key_configured_is_a_silent_no_op(self):
        with mock.patch.dict("os.environ", {"LMLOOP_WEB_VAPID_PRIVATE_KEY": "",
                                             "LMLOOP_WEB_VAPID_CONTACT": ""}, clear=False):
            self.assertEqual("", webpush.send(self.run_dict()))

    def test_no_subscribers_is_a_silent_no_op(self):
        empty_dir = tempfile.mkdtemp()
        fake_push = push_module.build(generate_pem(), "mailto:you@example.com",
                                       Path(empty_dir) / "push_subscriptions.json")
        with mock.patch.object(webpush, "_configured_push", return_value=fake_push):
            self.assertEqual("", webpush.send(self.run_dict()))

    def test_it_sends_the_title_and_body_notify_summarise_produced(self):
        store_dir = tempfile.mkdtemp()
        store_path = Path(store_dir) / "push_subscriptions.json"
        push_store.add(store_path, {"endpoint": "https://push.example/1",
                                     "keys": {"p256dh": "x", "auth": "y"}})
        fake_push = push_module.build(generate_pem(), "mailto:you@example.com", store_path)

        expected_title, expected_body, _tags, _priority = notify.summarise(self.run_dict())
        sent = {}

        def fake_webpush(subscription_info, data, **kwargs):
            sent["payload"] = json.loads(data)
            return mock.Mock(status_code=201)

        with mock.patch.object(webpush, "_configured_push", return_value=fake_push), \
             mock.patch("pywebpush.webpush", side_effect=fake_webpush):
            problem = webpush.send(self.run_dict())

        self.assertEqual("", problem)
        self.assertEqual(expected_title, sent["payload"]["title"])
        self.assertEqual(expected_body, sent["payload"]["body"])

    def test_a_gone_subscription_is_pruned_and_does_not_report_as_a_failure(self):
        from pywebpush import WebPushException

        store_dir = tempfile.mkdtemp()
        store_path = Path(store_dir) / "push_subscriptions.json"
        push_store.add(store_path, {"endpoint": "https://push.example/dead",
                                     "keys": {"p256dh": "x", "auth": "y"}})
        fake_push = push_module.build(generate_pem(), "mailto:you@example.com", store_path)

        error = WebPushException("gone", response=mock.Mock(status_code=410))

        with mock.patch.object(webpush, "_configured_push", return_value=fake_push), \
             mock.patch("pywebpush.webpush", side_effect=error):
            problem = webpush.send(self.run_dict())

        self.assertEqual("", problem)
        self.assertEqual([], push_store.all_subscriptions(store_path))

    def test_a_real_failure_is_reported_and_the_subscription_kept(self):
        from pywebpush import WebPushException

        store_dir = tempfile.mkdtemp()
        store_path = Path(store_dir) / "push_subscriptions.json"
        push_store.add(store_path, {"endpoint": "https://push.example/flaky",
                                     "keys": {"p256dh": "x", "auth": "y"}})
        fake_push = push_module.build(generate_pem(), "mailto:you@example.com", store_path)

        error = WebPushException("server error", response=mock.Mock(status_code=500))

        with mock.patch.object(webpush, "_configured_push", return_value=fake_push), \
             mock.patch("pywebpush.webpush", side_effect=error):
            problem = webpush.send(self.run_dict())

        self.assertNotEqual("", problem)
        self.assertEqual(1, len(push_store.all_subscriptions(store_path)))


if __name__ == "__main__":
    unittest.main()
