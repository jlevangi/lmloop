"""Whether omp's browser tool can reach the browser you think it can.

omp is the first agent lmloop drives that brings its own browser: a real
Chromium tab over the DevTools Protocol, no wrapper script and no extension.
That is worth having, and it fails in a way worth diagnosing early, because the
failure looks like the model being bad at its job.

Three facts decide it, all read out of omp v17.4.0 and confirmed against a live
endpoint:

* **omp attaches to an HTTP *discovery* endpoint, never to a websocket.**
  `app.cdp_url` and the `browser.cdpUrl` setting both go through a normaliser
  that rejects `ws://` and `wss://` by name -- "must be the HTTP CDP discovery
  endpoint (for example http://127.0.0.1:9222)".  A `wss://.../devtools/browser/<id>`
  URL, which is what a hosted browser usually hands out, is not a thing omp can
  be given.
* **It reaches that endpoint by string concatenation:** it polls
  ``${cdpUrl}/json/version`` until it answers, then hands the same URL to
  puppeteer's `browserURL`, which resolves `/json/version` against the origin.
  Either way a query string does not survive -- concatenation puts the path
  *after* it, and resolution drops it.  So an endpoint whose authentication is
  ``?token=...`` cannot be authenticated to by omp, and the symptom is a five
  second attach timeout with nothing else said.
* **A CDP endpoint is credentials.** Anything that can talk to it can read every
  page the browser has open.  So this module reports a token's *presence* and
  never its value, and nothing it returns is safe to widen.

What that adds up to: a loopback endpoint that answers `/json/version`
unauthenticated works natively today, and a token-authenticated one needs a
local shim that injects the credential and rewrites the websocket URL it
advertises.  This module says which one you have.  It does not open a tab: a
preflight that drives the browser is a preflight that can disturb whatever is
already using it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

# The wait omp gives an endpoint before it gives up (5s, polled every 150ms).
# Matching it means a preflight that passes is a preflight that predicts.
ATTACH_TIMEOUT_SECONDS = 5.0


def redact(url: str) -> str:
    """A CDP URL safe to print: same shape, no credential.

    Every query value goes, not just the ones named `token`.  A denylist of
    parameter names is a guess about someone else's proxy, and the cost of
    guessing wrong is a secret in a run log that lives for months.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable>"
    query = urllib.parse.urlencode(
        [(key, "<redacted>") for key, _ in urllib.parse.parse_qsl(parts.query)]
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, "")
    )


def preflight(cdp_url: str, timeout: float = ATTACH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """``(omp can attach, why)`` for a configured CDP endpoint.

    False is never fatal to a run.  An iteration whose browser is unreachable
    still reads, edits and commits like any other; it just cannot see the page.
    Saying so once at the start is worth more than an hour of the agent
    concluding it on its own.
    """
    if not cdp_url:
        return False, "no browser CDP endpoint configured"

    parts = urllib.parse.urlsplit(cdp_url)
    if parts.scheme in ("ws", "wss"):
        return False, (
            f"{parts.scheme}:// endpoint; omp attaches to an HTTP CDP discovery "
            "endpoint and rejects websocket URLs"
        )
    if parts.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parts.scheme or '(none)'}://"

    # The token question is decided before the request, because it is decided
    # whatever the request says: an endpoint that answers a credentialled probe
    # here is one omp will still meet uncredentialled.
    carries_query = bool(parts.query)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    probe = origin.rstrip("/") + "/json/version"

    try:
        with urllib.request.urlopen(probe, timeout=timeout) as response:
            version = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            detail = f"{redact(cdp_url)} requires authentication (HTTP {error.code})"
            if carries_query:
                # The exact case this module exists for.  The credential is
                # right there in the URL and omp cannot carry it: it appends
                # /json/version after the query, and puppeteer resolves the path
                # against the origin.  Neither form sends the token.
                return False, (
                    detail + "; the query credential does not survive omp's "
                    "attach -- a loopback shim that injects it is required"
                )
            return False, detail
        return False, f"{redact(cdp_url)}: HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return False, f"{redact(cdp_url)} unreachable: {error}"

    browser = version.get("Browser") or "unknown"
    if carries_query:
        # Reachable without the credential it carries: the query is decoration,
        # and omp dropping it changes nothing.
        return True, f"{browser} at {redact(cdp_url)} (credential not required)"
    return True, f"{browser} at {cdp_url}"
