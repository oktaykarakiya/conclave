"""Web layer: REST API, WebSocket stream, and SPA hosting."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
