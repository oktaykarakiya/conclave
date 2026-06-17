"""Unit tests for the NotificationSink seam and the webhook sink."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conclave.config import ConclaveConfig
from conclave.events import NotificationSink, WebhookSink, build_notification_sink
from conclave.events import notifications as notif_mod

# ---------------------------------------------------------------------------
# build_notification_sink — off by default, on only when a URL is configured
# ---------------------------------------------------------------------------


def test_sink_is_none_by_default() -> None:
    """With no webhook_url configured, no sink is built — notifications are inert."""
    assert build_notification_sink(ConclaveConfig()) is None


def test_sink_is_none_for_blank_url() -> None:
    """A whitespace-only URL is treated as unset (no sink)."""
    cfg = ConclaveConfig.model_validate({"notifications": {"webhook_url": "   "}})
    assert build_notification_sink(cfg) is None


def test_sink_built_when_url_configured() -> None:
    """A configured URL yields a WebhookSink satisfying the NotificationSink protocol."""
    cfg = ConclaveConfig.model_validate(
        {"notifications": {"webhook_url": "http://example.test/hook", "timeout_seconds": 5}}
    )
    sink = build_notification_sink(cfg)
    assert isinstance(sink, WebhookSink)
    assert isinstance(sink, NotificationSink)


# ---------------------------------------------------------------------------
# WebhookSink — POSTs JSON, swallows all delivery errors
# ---------------------------------------------------------------------------


async def test_webhook_sink_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify() POSTs the payload as JSON to the configured URL with the right headers."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.headers.get("Content-type")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(notif_mod.urllib.request, "urlopen", _fake_urlopen)

    sink = WebhookSink("http://example.test/hook", timeout_seconds=7.0)
    await sink.notify({"event": "task.done", "task_id": "t-1"})

    assert captured["url"] == "http://example.test/hook"
    assert captured["method"] == "POST"
    assert captured["body"] == {"event": "task.done", "task_id": "t-1"}
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 7.0


async def test_webhook_sink_swallows_delivery_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection/HTTP error inside notify() never propagates — best-effort contract."""

    def _boom(request: Any, timeout: float | None = None) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(notif_mod.urllib.request, "urlopen", _boom)

    sink = WebhookSink("http://unreachable.test/hook")
    # Must not raise.
    await sink.notify({"event": "task.failed", "task_id": "t-2"})
