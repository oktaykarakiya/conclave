"""JSON-RPC 2.0 MCP protocol dispatcher and client snippet generator."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import ValidationError

from .. import __version__
from ..db import repositories as repo
from ..events import EventType
from ..runtime import Daemon
from .tools import (
    MCP_TOOLS,
    PILOT_GUIDE_URI,
    McpToolError,
    call_mcp_tool,
    load_pilot_brief,
    load_pilot_guide,
)

logger = logging.getLogger("conclave.mcp")

_DEFAULT_PROTOCOL_VERSION = "2024-11-05"

_OBSERVABILITY_READ_TOOLS: frozenset[str] = frozenset(
    {
        "conclave_list_interactions",
        "conclave_get_interaction",
        "conclave_query_interactions",
        "conclave_get_observability_stats",
        "conclave_get_reload_status",
    }
)


def mcp_connect_snippets(base_url: str = "http://127.0.0.1:8700") -> dict[str, Any]:
    """Return ready-to-copy MCP configuration snippets for OpenCode and Claude Code."""
    mcp_url = f"{base_url.rstrip('/')}/mcp"
    return {
        "mcp_url": mcp_url,
        "tool_count": len(MCP_TOOLS),
        "tools": [t["name"] for t in MCP_TOOLS],
        "opencode_remote_json": {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "conclave": {
                    "type": "remote",
                    "url": mcp_url,
                    "enabled": True,
                }
            },
        },
        "opencode_stdio_json": {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "conclave": {
                    "type": "local",
                    "command": ["python", "-m", "conclave.mcp"],
                    "environment": {"CONCLAVE_URL": base_url.rstrip("/")},
                    "enabled": True,
                }
            },
        },
        "claude_code_http_cli": f"claude mcp add --transport http conclave {mcp_url}",
        "claude_code_stdio_cli": "claude mcp add conclave -- python -m conclave.mcp",
        "claude_mcp_json": {
            "mcpServers": {
                "conclave": {
                    "type": "http",
                    "url": mcp_url,
                }
            }
        },
    }


def _jsonrpc_result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _parse_message(payload: dict[str, Any]) -> tuple[str, Any, dict[str, Any], bool]:
    """``(method, id, params, is_notification)`` of one JSON-RPC message."""
    method = str(payload.get("method", ""))
    msg_id = payload.get("id")
    params_raw = payload.get("params")
    params = params_raw if isinstance(params_raw, dict) else {}
    # Notifications carry no id and expect no response object.
    notification = "id" not in payload or (msg_id is None and method.startswith("notifications/"))
    return method, msg_id, params, notification


def _pilot_guide_resource() -> dict[str, Any]:
    return {
        "uri": PILOT_GUIDE_URI,
        "name": "Conclave Pilot Guide",
        "description": (
            "How to operate Conclave over MCP: multi-agent flows, review/merge, "
            "model strategy, pitfalls, tool index"
        ),
        "mimeType": "text/markdown",
    }


def _static_result(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """The result of a request answered from the packaged definitions alone (no daemon state),
    or ``None`` when the request needs the daemon."""
    if method == "initialize":
        return {
            "protocolVersion": str(params.get("protocolVersion") or _DEFAULT_PROTOCOL_VERSION),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {
                "name": "conclave-mcp",
                "version": __version__,
            },
            "instructions": load_pilot_brief(),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": MCP_TOOLS}
    if method == "resources/read" and str(params.get("uri", "")) == PILOT_GUIDE_URI:
        return {
            "contents": [
                {
                    "uri": PILOT_GUIDE_URI,
                    "mimeType": "text/markdown",
                    "text": load_pilot_guide(),
                }
            ]
        }
    return None


def handle_offline_mcp_message(
    payload: dict[str, Any], unreachable: str
) -> dict[str, Any] | None:
    """Answer one JSON-RPC 2.0 MCP message while the daemon cannot be reached.

    Used by the stdio bridge (``conclave-mcp``), which never opens Conclave's database:
    ``initialize``, ``ping``, ``tools/list``, ``resources/list`` (the pilot guide only) and
    ``resources/read`` of the pilot guide get the daemon's own answers. A tool call gets an
    ``isError`` result and every other request a JSON-RPC error, both with the *unreachable*
    text; notifications get no response.
    """
    method, msg_id, params, notification = _parse_message(payload)
    if notification:
        return None
    result = _static_result(method, params)
    if result is not None:
        return _jsonrpc_result(msg_id, result)
    if method == "resources/list":
        return _jsonrpc_result(msg_id, {"resources": [_pilot_guide_resource()]})
    if method == "tools/call":
        return _jsonrpc_result(
            msg_id, {"content": [{"type": "text", "text": unreachable}], "isError": True}
        )
    return _jsonrpc_error(msg_id, -32603, unreachable)


async def handle_mcp_message(
    daemon: Daemon, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 MCP message and return a response dict (or None)."""
    method, msg_id, params, notification = _parse_message(payload)
    if notification:
        return None

    result = _static_result(method, params)
    if result is not None:
        return _jsonrpc_result(msg_id, result)

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        raw_args = params.get("arguments")
        tool_args = raw_args if isinstance(raw_args, dict) else {}
        t0 = time.monotonic()
        raw_pid = tool_args.get("project_id")
        pid = str(raw_pid).strip() if isinstance(raw_pid, str) and raw_pid.strip() else None
        if pid is not None and pid.lower() == "global":
            pid = None
        raw_tid = tool_args.get("task_id")
        tid = str(raw_tid).strip() if isinstance(raw_tid, str) and raw_tid.strip() else None
        if pid is None and tid is not None:
            task_obj = await repo.get_task(daemon.db, tid)
            if task_obj is not None:
                pid = task_obj.project_id
        user_prompt_str = json.dumps(tool_args)
        should_log = tool_name not in _OBSERVABILITY_READ_TOOLS
        try:
            output = await call_mcp_tool(daemon, tool_name, tool_args)
            duration = round(max(0.0, time.monotonic() - t0), 4)
            await daemon.bus.emit(
                type=EventType.mcp_tool_called,
                payload={"tool": tool_name, "ok": True, "project_id": pid, "task_id": tid},
            )
            if should_log:
                assistant_text = json.dumps(output)[:4000]
                log_row = await repo.record_interaction_log(
                    daemon.db,
                    source="mcp",
                    agent="mcp",
                    model="mcp/jsonrpc",
                    title=f"MCP: {tool_name}",
                    user_prompt=user_prompt_str,
                    messages=[
                        {"role": "user", "text": user_prompt_str},
                        {"role": "assistant", "text": assistant_text},
                    ],
                    tool_calls_count=1,
                    message_count=2,
                    tool_breakdown={tool_name: 1},
                    duration=duration,
                    project_id=pid,
                    task_id=tid,
                )
                await daemon.bus.emit(
                    type=EventType.interaction_logged,
                    payload={
                        "session_id": log_row.session_id,
                        "source": "mcp",
                        "tool": tool_name,
                        "agent": "mcp",
                        "project_id": pid,
                    },
                )
            return _jsonrpc_result(
                msg_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": output
                            if isinstance(output, str)
                            else json.dumps(output, indent=2),
                        }
                    ],
                    "isError": False,
                },
            )
        except (McpToolError, ValidationError, ValueError, TypeError) as exc:
            duration = round(max(0.0, time.monotonic() - t0), 4)
            err_text = f"Error: {exc}"
            if should_log:
                await repo.record_interaction_log(
                    daemon.db,
                    source="mcp",
                    agent="mcp",
                    model="mcp/jsonrpc",
                    title=f"MCP: {tool_name}",
                    user_prompt=user_prompt_str,
                    messages=[
                        {"role": "user", "text": user_prompt_str},
                        {"role": "assistant", "text": err_text[:4000]},
                    ],
                    tool_calls_count=1,
                    message_count=2,
                    tool_breakdown={tool_name: 1},
                    duration=duration,
                    project_id=pid,
                    task_id=tid,
                )
            await daemon.bus.emit(
                type=EventType.mcp_tool_called,
                payload={"tool": tool_name, "ok": False, "error": str(exc)},
            )
            return _jsonrpc_result(
                msg_id,
                {
                    "content": [{"type": "text", "text": err_text}],
                    "isError": True,
                },
            )
        except Exception as exc:
            logger.exception("Unhandled error in MCP tool %s", tool_name)
            await daemon.bus.emit(
                type=EventType.mcp_tool_called,
                payload={"tool": tool_name, "ok": False, "error": repr(exc)},
            )
            return _jsonrpc_result(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"Internal error: {exc!r}"}],
                    "isError": True,
                },
            )

    if method == "resources/list":
        projects = await repo.list_projects(daemon.db, limit=100)
        resources: list[dict[str, Any]] = [_pilot_guide_resource()]
        resources.extend(
            {
                "uri": f"conclave://projects/{p.id}",
                "name": f"Project: {p.name}",
                "description": f"Repository at {p.path} (branch {p.default_branch})",
                "mimeType": "application/json",
            }
            for p in projects
        )
        return _jsonrpc_result(msg_id, {"resources": resources})

    if method == "resources/templates/list":
        return _jsonrpc_result(
            msg_id,
            {
                "resourceTemplates": [
                    {
                        "uriTemplate": "conclave://projects/{project_id}",
                        "name": "Conclave Project",
                        "mimeType": "application/json",
                    },
                    {
                        "uriTemplate": "conclave://tasks/{task_id}",
                        "name": "Conclave Task",
                        "mimeType": "application/json",
                    },
                ]
            },
        )

    if method == "resources/read":
        uri = str(params.get("uri", ""))
        if uri.startswith("conclave://projects/"):
            pid = uri.removeprefix("conclave://projects/").strip("/")
            proj = await repo.get_project(daemon.db, pid)
            if proj is None:
                return _jsonrpc_error(msg_id, -32602, f"Resource not found: {uri}")
            tasks = await repo.list_tasks(daemon.db, pid, limit=50)
            body = json.dumps(
                {
                    "project": proj.model_dump(mode="json"),
                    "tasks": [t.model_dump(mode="json") for t in tasks],
                },
                indent=2,
            )
            return _jsonrpc_result(
                msg_id,
                {
                    "contents": [
                        {"uri": uri, "mimeType": "application/json", "text": body}
                    ]
                },
            )
        if uri.startswith("conclave://tasks/"):
            tid = uri.removeprefix("conclave://tasks/").strip("/")
            task = await repo.get_task(daemon.db, tid)
            if task is None:
                return _jsonrpc_error(msg_id, -32602, f"Resource not found: {uri}")
            checkpoints = await repo.list_task_checkpoints(daemon.db, task.id)
            verdicts = await repo.list_verdicts(daemon.db, task.id, limit=100)
            body = json.dumps(
                {
                    "task": task.model_dump(mode="json"),
                    "checkpoints": [cp.model_dump(mode="json") for cp in checkpoints],
                    "verdicts": [v.model_dump(mode="json") for v in verdicts],
                },
                indent=2,
            )
            return _jsonrpc_result(
                msg_id,
                {
                    "contents": [
                        {"uri": uri, "mimeType": "application/json", "text": body}
                    ]
                },
            )
        return _jsonrpc_error(msg_id, -32602, f"Unknown resource URI: {uri}")

    return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")
