"""Who is allowed to drive the dashboard, in three deployments.

The dashboard can start and stop agents, delete archives and open pull
requests, so "who is asking" is not a display concern.  There are three honest
answers, and lmloop supports all three rather than assuming the one this was
first deployed with:

* `none` -- nobody is asked, and the server binds loopback only.  Reach it
  over an SSH tunnel.  The right answer for a laptop.
* `proxy` -- identity is asserted by a reverse proxy that has already
  authenticated the request, in a header.  The right answer behind an existing
  ingress that does SSO, and the common one: oauth2-proxy, Authelia, Cloudflare
  Access, an nginx `auth_request`.
* `oidc` -- the dashboard runs the login itself.  Generic OIDC discovery, not
  any particular provider: Keycloak is one issuer among many and nothing here
  knows its name.

The mode is chosen explicitly by `LMLOOP_WEB_AUTH_MODE`, or inferred from
whether OIDC is configured, which keeps every existing deployment working
without being told about this.

**A network bind without an identity boundary is refused**, in every mode.
That is the invariant the modes exist to serve, not a property of one of them.

`proxy` mode has a trap this is built around: a header is only evidence if
nothing else can set it.  Anyone who can reach the port directly can send
`X-Forwarded-User: admin`, so the header is read *only* when the connection
comes from an address in `LMLOOP_WEB_TRUSTED_PROXIES`, and configuring the
mode without that list is refused rather than defaulted.

lmloop itself is stdlib-only and stays that way: PyJWT and requests are
imported here and nowhere else, and a missing one disables OIDC rather than
breaking the loop.  `none` and `proxy` need neither.

Install with the interpreter that will run the server:

    python3 -m pip install "PyJWT[crypto]>=2.7,<3" "requests>=2.31,<3"
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



LOOPBACK = ("127.0.0.1", "localhost", "::1", "")


class NoAuth:
    """Nobody is asked, so nothing may be reached from off the machine."""

    mode = "none"
    trusted = False        # no identity boundary: bind loopback only
    interactive = False    # no login or logout to route
    enabled = False        # OIDC-specific branches stay off
    session_seconds = 0

    def session_for(self, handler) -> dict:
        # A CSRF token guards a session cookie against another origin.  There
        # is no session cookie here and no origin but this machine, so there is
        # nothing for one to protect; saying so is better than minting a token
        # that means nothing.
        return {"name": "local", "csrf": "disabled"}


class ProxyAuth:
    """Identity asserted by a reverse proxy that already authenticated it.

    The header is evidence only because of where it came from.  A request that
    reaches this port from anywhere but a trusted proxy is anonymous however it
    is decorated -- otherwise the mode is a way of asking attackers to name
    themselves.
    """

    mode = "proxy"
    trusted = True
    interactive = False
    enabled = False
    session_seconds = 0

    def __init__(self, header: str, trusted_proxies, display_header: str = ""):
        self.header = header or "X-Forwarded-User"
        self.display_header = display_header
        self.trusted_proxies = tuple(p for p in trusted_proxies if p)

    def peer_is_trusted(self, peer: str) -> bool:
        return peer in self.trusted_proxies

    def session_for(self, handler) -> dict | None:
        peer = handler.client_address[0] if handler.client_address else ""
        if not self.peer_is_trusted(peer):
            return None
        user = (handler.headers.get(self.header) or "").strip()
        if not user:
            return None
        name = (handler.headers.get(self.display_header) or "").strip() if self.display_header else ""
        # No CSRF token: there is no ambient credential for another origin to
        # ride on.  The proxy authenticated this request; a browser at
        # evil.example cannot make the proxy assert a user it has not logged in.
        return {"name": name or user, "user": user, "csrf": "disabled"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class OIDC:
    """The dashboard runs the login itself, against any OIDC issuer.

    Generic on purpose: discovery is `/.well-known/openid-configuration` and
    nothing here knows a provider by name.
    """

    mode = "oidc"
    interactive = True     # owns /login, /oauth/* and /logout

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
    def trusted(self):
        """Is there an identity boundary?  Same question `enabled` answers for
        this mode, named the way the other two answer it."""
        return self.enabled

    def session_for(self, handler):
        return self.session(handler.cookies().get("lmloop_session"))

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


def build(settings: dict):
    """The auth for one deployment, from its settings.

    `mode` is explicit when given and inferred otherwise: a deployment that
    configured OIDC before this existed means `oidc`, and one that did not
    means `none`.  Nobody has to be told about the modes for their existing
    setup to keep working.
    """
    mode = (settings.get("mode") or "").strip().lower()
    explicit = bool(mode)
    if not mode:
        mode = "oidc" if settings.get("oidc_issuer") else "none"

    if mode == "none":
        return NoAuth()
    if mode == "proxy":
        proxies = settings.get("trusted_proxies") or ()
        if not proxies:
            raise SystemExit(
                "lmloop web: auth mode `proxy` trusts an identity header, and a\n"
                "  header is only evidence if nothing else can set it.  Set\n"
                "  LMLOOP_WEB_TRUSTED_PROXIES to the addresses your proxy connects\n"
                "  from, or anyone who can reach this port can name themselves."
            )
        return ProxyAuth(
            settings.get("proxy_header", ""), proxies,
            settings.get("proxy_display_header", ""),
        )
    if mode == "oidc":
        if not AVAILABLE:
            if explicit:
                raise SystemExit(
                    "lmloop web: auth mode `oidc` needs PyJWT and requests, which\n"
                    "  are not installed for this interpreter.  Install them, or use\n"
                    "  LMLOOP_WEB_AUTH_MODE=proxy behind an ingress that authenticates."
                )
            # Inferred, not asked for: a deployment that configured OIDC and
            # then lost the libraries used to fall back to loopback rather than
            # refuse to start, and taking that away would break it on an
            # upgrade it did not ask for.  Loud, and still safe -- the bind
            # check below refuses the network either way.
            print("lmloop web: OIDC is configured but PyJWT/requests are not"
                  " installed;\n  falling back to auth mode `none`, which binds"
                  " loopback only.")
            return NoAuth()
        return OIDC(
            settings.get("oidc_issuer", ""),
            settings.get("oidc_client_id", ""),
            settings.get("oidc_client_secret", ""),
            settings.get("public_url", ""),
            settings.get("session_secret", ""),
            int(settings.get("session_hours", 12) or 12),
        )
    raise SystemExit(
        f"lmloop web: unknown auth mode {mode!r}; known: none, proxy, oidc"
    )
