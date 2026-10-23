"""Model Context Protocol (MCP) server for Conclave (HTTP + stdio transports)."""

from __future__ import annotations

from .server import handle_mcp_message, mcp_connect_snippets
from .tools import MCP_TOOLS, call_mcp_tool

__all__ = [
    "MCP_TOOLS",
    "call_mcp_tool",
    "handle_mcp_message",
    "mcp_connect_snippets",
]
