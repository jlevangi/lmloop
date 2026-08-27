"""Device tokens: read-only API access for clients outside a browser session.

The three modes in `web/auth.py` answer "who may load the dashboard." This
answers a narrower question, asked only for `GET /api/*` -- "may this request
read run state" -- for a client that never has a cookie jar: a background
service or a periodic job on a phone, polling for a run's progress or
completion while nothing with a session is open.

**This can never grant write access, by construction rather than by a flag.**
`web/server.py`'s `do_POST` calls only `self.require_auth(api=True)`, which
never inspects `Authorization` -- a device token cannot satisfy it, so it
cannot start, stop, pause, archive, delete or PR a run, however it is
configured, regardless of `LMLOOP_WEB_READ_ONLY`. A phone is a more likely
loss than a laptop with an SSH tunnel open; read access is the most this
mechanism can ever grant, and that is deliberate, not an oversight to close
later. Do not add a `do_POST` path that consults `device_session()`.

Configure with `LMLOOP_WEB_DEVICE_TOKENS`, comma-separated `label=reference`
pairs. Each reference is resolved through `config.secret` -- `env:NAME`,
`file:PATH`, `!command`, or a literal -- the same indirection every other
credential in this project uses, so a token never has to sit in a config file
that gets copied into a repo or pasted into an issue. Generate one with:

    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
"""

from __future__ import annotations

import hmac

import config as config_module


class DeviceTokens:
    """A small set of long-lived bearer tokens, one per device."""

    def __init__(self, tokens: dict[str, str]):
        self._tokens = tokens  # label -> resolved token value

    def match(self, header_value: str) -> str | None:
        """The label of the device this header authenticates as, or None.

        Checked against every configured token via `hmac.compare_digest`
        rather than a dict lookup, so which tokens exist cannot be inferred
        from how long a mismatch took to reject.
        """
        if not header_value or not header_value.startswith("Bearer "):
            return None
        candidate = header_value[len("Bearer "):].strip()
        if not candidate:
            return None
        for label, token in self._tokens.items():
            if hmac.compare_digest(candidate, token):
                return label
        return None

    def __bool__(self) -> bool:
        return bool(self._tokens)


def build(raw: str) -> DeviceTokens:
    """Parse `LMLOOP_WEB_DEVICE_TOKENS` into a `DeviceTokens`.

    A label whose reference resolves to nothing is skipped and reported, not
    fatal: one bad device token should deny one device, not the whole
    dashboard. A `!command`/`file:` reference may itself legally contain `=`,
    so each entry is only ever split on its *first* `=`.
    """
    tokens: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            print(f"lmloop web: ignoring malformed LMLOOP_WEB_DEVICE_TOKENS "
                  f"entry {entry!r}; expected label=reference")
            continue
        label, reference = entry.split("=", 1)
        label = label.strip()
        value = config_module.secret(reference.strip())
        if not value:
            print(f"lmloop web: device token {label!r} resolved to nothing; "
                  "that device is denied, not the dashboard")
            continue
        tokens[label] = value
    return DeviceTokens(tokens)
