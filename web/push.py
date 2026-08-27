"""Web Push (VAPID) for the browser PWA.

The dashboard's only notification path used to be `notify.py`'s one-shot ntfy
push, sent from the loop process and entirely invisible to anyone who only
has the PWA installed. This lets a subscribed browser get a native push
straight from the platform's own push service instead -- no third-party
server required, though ntfy keeps working unchanged for whoever already has
it configured; the two are independent (see `webpush.py`).

Like `web/auth.py`'s OIDC support, this needs a library this project does not
ship with by default -- `pywebpush` (which pulls in `py_vapid`) -- and a
missing one disables push rather than breaking the server. Install with the
interpreter that runs it:

    python3 -m pip install "pywebpush>=2.0,<3"

The VAPID private key is deliberately *not* auto-generated and written to
disk: it is operator-provided, the same way `LMLOOP_WEB_SESSION_SECRET` is,
resolved through `config.secret` (`env:`, `file:`, `!command`, or a literal)
so it never has to sit in a config file that gets copied into a repo.
Generate a keypair once with:

    python3 -c "from py_vapid import Vapid01; v = Vapid01(); v.generate_keys(); print(v.private_pem().decode())"
"""

from __future__ import annotations

from pathlib import Path

from web import push_store

try:  # noqa: SIM105 - the failure is meaningful, see AVAILABLE
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    from py_vapid.utils import b64urlencode
    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host interpreter
    Vapid01 = serialization = b64urlencode = None
    AVAILABLE = False


class WebPush:
    """One deployment's push configuration: a VAPID key pair, a contact for
    the mandatory `sub` claim, and where subscriptions live.

    `enabled` is false whenever any prerequisite is missing -- the library,
    the private key, or the contact -- so callers can treat "push is off"
    uniformly instead of checking each cause separately.
    """

    def __init__(self, private_key_pem: str, contact: str, store_path: Path):
        self.contact = contact
        self.store_path = store_path
        self.vapid = None
        self._public_key = ""
        if not (AVAILABLE and private_key_pem and contact):
            return
        try:
            vapid = Vapid01.from_pem(private_key_pem.encode())
            raw = vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            self._public_key = b64urlencode(raw)
            self.vapid = vapid
        except Exception as error:  # noqa: BLE001 - a bad key disables push, not the server
            print(f"lmloop web: VAPID private key could not be loaded ({error});"
                  " push notifications are disabled")

    @property
    def enabled(self) -> bool:
        return self.vapid is not None

    @property
    def public_key(self) -> str:
        """The `applicationServerKey` a browser's `PushManager.subscribe`
        needs, base64url-encoded, or "" when push is not configured."""
        return self._public_key

    def subscriptions(self) -> list[dict]:
        return push_store.all_subscriptions(self.store_path)


def build(private_key_pem: str, contact: str, store_path: Path | None = None) -> WebPush:
    return WebPush(private_key_pem, contact, store_path or push_store.default_path())
