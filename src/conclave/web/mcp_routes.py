"""Streamable HTTP Model Context Protocol (MCP) endpoints (`/mcp` and `/api/mcp/info`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..mcp import handle_mcp_message, mcp_connect_snippets
from ..runtime import Daemon
from .deps import get_daemon

router = APIRouter()


@router.get("/api/mcp/info")
async def get_mcp_info(request: Request) -> dict[str, Any]:
    """Return MCP tool catalog metadata and copy-ready snippets for OpenCode & Claude Code."""
    host = request.headers.get("host", "127.0.0.1:8700")
    scheme = request.url.scheme or "http"
    return mcp_connect_snippets(f"{scheme}://{host}")


@router.get("/mcp", include_in_schema=False)
async def mcp_get() -> JSONResponse:
    """Refuse the optional server-to-client SSE stream: ``405`` with ``Allow: POST``.

    The Streamable HTTP transport lets a client open that stream with ``GET``, and the server
    must answer it with ``text/event-stream`` or ``405``. Conclave never sends anything
    unprompted, so it answers ``405`` whatever the ``Accept`` header: a ``200`` reads as a
    stream that ended at once, which clients reopen every second. Without this route the SPA
    mount at ``/`` would answer ``404``. The metadata is at ``GET /api/mcp/info``.
    """
    return JSONResponse(
        status_code=405,
        content={
            "detail": "Method Not Allowed: send JSON-RPC requests with POST /mcp; "
            "server metadata is at GET /api/mcp/info"
        },
        headers={"Allow": "POST"},
    )


@router.post("/mcp")
async def mcp_post(
    request: Request,
    daemon: Daemon = Depends(get_daemon),
) -> Response:
    """Handle JSON-RPC 2.0 MCP requests (single message or batch)."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON-RPC payload") from exc

    if isinstance(body, list):
        responses: list[dict[str, Any]] = []
        for item in body:
            if isinstance(item, dict):
                res = await handle_mcp_message(daemon, item)
                if res is not None:
                    responses.append(res)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(content=responses)

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON-RPC payload must be an object or array")

    reply = await handle_mcp_message(daemon, body)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(content=reply)
