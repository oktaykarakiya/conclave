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


@router.get("/mcp")
async def mcp_get_info(request: Request) -> dict[str, Any]:
    """Return server metadata on GET /mcp."""
    host = request.headers.get("host", "127.0.0.1:8700")
    scheme = request.url.scheme or "http"
    return mcp_connect_snippets(f"{scheme}://{host}")


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
