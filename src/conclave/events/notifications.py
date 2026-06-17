"""Outbound notifications for terminal task events.

A deliberately small seam: a :class:`NotificationSink` Protocol plus one webhook
implementation that POSTs a compact JSON payload to a configured URL when a task
reaches a terminal state (done/failed). It is inert unless a URL is configured and
strictly best-effort — a sink failure must never affect task processing — so the
orchestrator can fire it and forget it.

The webhook uses stdlib ``urllib.request`` (no runtime HTTP dependency) executed in a
worker thread via :func:`asyncio.to_thread`, so the blocking socket call never stalls
the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

from ..config import ConclaveConfig

logger = logging.getLogger("conclave.events.notifications")


@runtime_checkable
class NotificationSink(Protocol):
    """A sink that delivers a small notification payload somewhere out-of-process.

    Implementations MUST be best-effort: :meth:`notify` should swallow its own delivery
    errors so a caller can fire it without a guard. The orchestrator still wraps calls
    defensively, but the contract keeps notification failures from ever surfacing.
    """

    async def notify(self, payload: dict[str, Any]) -> None: ...


class WebhookSink:
    """POSTs the notification payload as JSON to a fixed URL.

    All delivery errors (connection refused, timeout, non-2xx, malformed URL) are caught
    and logged at WARNING — the sink never raises. The request runs in a worker thread so
    the synchronous ``urllib`` call does not block the event loop.
    """

    def __init__(self, url: str, *, timeout_seconds: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def notify(self, payload: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._post, payload)
        except Exception:
            # Defense in depth: _post already swallows delivery errors, but a failure to
            # even schedule the thread (or serialise the payload) must not escape either.
            logger.warning("notification webhook delivery failed for %s", self._url, exc_info=True)

    def _post(self, payload: dict[str, Any]) -> None:
        # The URL is operator-configured project config (not user input), POSTed as JSON.
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout):
                pass
        except (urllib.error.URLError, OSError, ValueError):
            logger.warning("notification webhook POST to %s failed", self._url, exc_info=True)


def build_notification_sink(config: ConclaveConfig) -> NotificationSink | None:
    """Build the configured notification sink, or ``None`` when notifications are off.

    Reads ``config.notifications``; a sink is returned only when a non-empty
    ``webhook_url`` is set, so the feature is fully inert by default.
    """
    settings = config.notifications
    url = (settings.webhook_url or "").strip()
    if not url:
        return None
    return WebhookSink(url, timeout_seconds=settings.timeout_seconds)
