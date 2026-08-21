"""OIDC login for the web UI, ported from the predecessor dashboard unchanged in substance.

lmloop itself is stdlib-only and stays that way: PyJWT and requests are
imported here and nowhere else, and a missing one disables authentication
rather than breaking the loop.  `lmloop web` refuses to bind anything but
localhost when auth is unavailable -- an unauthenticated dashboard that can't
start and stop agents has no business on a network.

Install with the interpreter that will run the server:

    /usr/bin/python3 -m pip install "PyJWT[crypto]>=2.7,<3" "requests>=2.31,<3"
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

try:  # noqa: SIM105 - the failure is meaningful, see AVAILABLE
    import jwt
    import requests
    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host interpreter
    jwt = requests = None
    AVAILABLE = False



def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class OIDC:
    def __init__(self, issuer, client_id, client_secret, public_url, session_secret, session_hours=12):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.public_url = public_url.rstrip("/")
        self.session_secret = session_secret.encode()
        self.session_seconds = session_hours * 3600
        self._metadata = None

    @property
    def enabled(self):
        return all((self.issuer, self.client_id, self.client_secret, self.public_url, self.session_secret))

    @property
    def callback_url(self):
        return self.public_url + "/oauth/callback"

    def metadata(self):
        if self._metadata is None:
            response = requests.get(self.issuer + "/.well-known/openid-configuration", timeout=10)
            response.raise_for_status()
            self._metadata = response.json()
            if self._metadata.get("issuer") != self.issuer:
                raise ValueError("OIDC discovery issuer mismatch")
        return self._metadata

    def sign(self, value):
        payload = _b64(json.dumps(value, separators=(",", ":")).encode())
        signature = _b64(hmac.new(self.session_secret, payload.encode(), hashlib.sha256).digest())
        return payload + "." + signature

    def unsign(self, value):
        try:
            payload, signature = value.split(".", 1)
            expected = _b64(hmac.new(self.session_secret, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            result = json.loads(_unb64(payload))
            if result.get("exp", 0) < time.time():
                return None
            return result
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def begin(self):
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        transaction = self.sign({"state": state, "nonce": nonce, "verifier": verifier, "exp": time.time() + 600})
        query = urlencode({
            "client_id": self.client_id, "response_type": "code", "scope": "openid profile email",
            "redirect_uri": self.callback_url, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return self.metadata()["authorization_endpoint"] + "?" + query, transaction

    def callback(self, code, state, transaction):
        pending = self.unsign(transaction)
        if not pending or not state or not hmac.compare_digest(state, pending.get("state", "")):
            raise ValueError("invalid OIDC state")
        response = requests.post(self.metadata()["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.callback_url,
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code_verifier": pending["verifier"],
        }, timeout=10)
        response.raise_for_status()
        token = response.json()["id_token"]
        key = jwt.PyJWKClient(self.metadata()["jwks_uri"]).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer)
        if not hmac.compare_digest(claims.get("nonce", ""), pending["nonce"]):
            raise ValueError("invalid OIDC nonce")
        session = {"sub": claims["sub"], "name": claims.get("name") or claims.get("preferred_username") or claims["sub"], "email": claims.get("email", ""), "csrf": secrets.token_urlsafe(24), "exp": time.time() + self.session_seconds}
        return self.sign(session)

    def session(self, cookie):
        return self.unsign(cookie) if cookie else None

    def logout_url(self):
        return self.metadata().get("end_session_endpoint", self.public_url)
