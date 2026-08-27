"""Push a run's finish to every browser subscribed to Web Push.

Sibling to `notify.py`, and deliberately similar in shape: the same "never
fails a run" guarantee, the same one-push-at-the-end design (see notify.py's
module docstring for why), and the same title/body -- reused from
`notify.summarise` rather than duplicated, so a Web Push notification and an
ntfy notification about the same run always agree.

Independent of ntfy: this fires whenever a browser has subscribed, regardless
of whether `[notify]` is configured, and vice versa. A deployment can have
either, both, or neither.

Reads the same `LMLOOP_WEB_*` environment `lmloop-web` itself does. A run
launched from the dashboard inherits it (`web/service.py`'s `start_run` and
`continue` both spawn the loop with plain `subprocess.run`/`Popen` and no
`env=`, so the parent's environment carries over); a run started directly
from the CLI, outside the dashboard, has none of it, and this is silently a
no-op -- the same as ntfy already is when `[notify]` is unset.
"""

from __future__ import annotations

import json
import os

import config
import notify
from web import push as push_module
from web import push_store


def _configured_push() -> push_module.WebPush:
    private_key = config.secret(os.environ.get("LMLOOP_WEB_VAPID_PRIVATE_KEY", ""))
    contact = config.reference(os.environ.get("LMLOOP_WEB_VAPID_CONTACT", ""))
    return push_module.build(private_key, contact)


def send(run: dict) -> str:
    """Push one 'run finished' notification to every subscribed browser.

    Returns "" on success -- including "nothing to do": push not configured,
    or nobody subscribed -- or a description of the last failure. Mirrors
    `notify.send`'s contract so a caller can announce it the same way.
    """
    push = _configured_push()
    if not push.enabled:
        return ""
    subscriptions = push.subscriptions()
    if not subscriptions:
        return ""

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return "pywebpush not installed"

    title, body, tags, priority = notify.summarise(run)
    url = config.reference(run.get("dashboard_url", ""))
    if url and run.get("project") and run.get("run_id"):
        url = f"{url.rstrip('/')}/#{run['project']}/{run['run_id']}"
    payload = json.dumps({
        "title": title, "body": body, "url": url,
        "project": run.get("project", ""), "run_id": run.get("run_id", ""),
    })

    last_problem = ""
    gone: list[str] = []
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=push.vapid,
                vapid_claims={"sub": push.contact},
                ttl=86400,
            )
        except WebPushException as error:
            status = getattr(error.response, "status_code", None)
            if status in (404, 410):
                # The browser dropped this subscription; stop trying it rather
                # than fail every future run on a push nobody will receive.
                gone.append(subscription.get("endpoint", ""))
            else:
                last_problem = str(error)
        except (OSError, ValueError) as error:
            last_problem = str(error)

    for endpoint in gone:
        push_store.remove(push.store_path, endpoint)

    return last_problem
