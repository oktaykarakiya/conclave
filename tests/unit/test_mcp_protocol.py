"""Unit tests for MCP stdio proxy reliability and MCP JSON-RPC protocol completeness."""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conclave.db import CheckpointStatus, Database
from conclave.db import repositories as repo
from conclave.events import EventType
from conclave.mcp.__main__ import _forward_http, _run_stdio
from conclave.mcp.server import handle_mcp_message
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon


class _StubProvider(Provider):
    name = "stub"

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        return AgentResult(ok=True, text='```json\n{"verdict": "pass", "reason": "ok"}\n```')


def _make_http_error(code: int, reason: str, body: bytes) -> urllib.error.HTTPError:
    hdrs = Message()
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8700/mcp",
        code=code,
        msg=reason,
        hdrs=hdrs,
        fp=io.BytesIO(body),
    )


def test_forward_http_timeout_returns_jsonrpc_error_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutError or URLError(TimeoutError) must return a -32603 JSON-RPC error
    and inspect CONCLAVE_MCP_TIMEOUT (default 300.0)."""
    captured_timeouts: list[float] = []

    def fake_urlopen(req: Any, timeout: float = 0.0) -> Any:
        captured_timeouts.append(timeout)
        raise TimeoutError("read operation timed out")

    monkeypatch.delenv("CONCLAVE_MCP_TIMEOUT", raising=False)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _forward_http("http://127.0.0.1:8700/mcp", {"jsonrpc": "2.0", "id": 42})

    assert captured_timeouts == [300.0]
    assert isinstance(res, dict)
    assert res["jsonrpc"] == "2.0"
    assert res["id"] == 42
    assert res["error"]["code"] == -32603
    assert "timed out" in res["error"]["message"]

    # Also test URLError wrapping TimeoutError with custom CONCLAVE_MCP_TIMEOUT
    monkeypatch.setenv("CONCLAVE_MCP_TIMEOUT", "120.5")

    def fake_urlopen_url_timeout(req: Any, timeout: float = 0.0) -> Any:
        captured_timeouts.append(timeout)
        raise urllib.error.URLError(TimeoutError("socket timed out"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen_url_timeout):
        res2 = _forward_http({"jsonrpc": "2.0", "id": "req-2"})

    assert captured_timeouts[-1] == 120.5
    assert isinstance(res2, dict)
    assert res2["id"] == "req-2"
    assert res2["error"]["code"] == -32603


def test_forward_http_error_returns_body_or_jsonrpc_error_without_raising() -> None:
    """HTTPError must never raise; returns parsed JSON-RPC body if valid, else -32603."""
    jsonrpc_err_body = json.dumps(
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32600, "message": "Invalid Request"}}
    ).encode("utf-8")
    err1 = _make_http_error(400, "Bad Request", jsonrpc_err_body)
    with patch("urllib.request.urlopen", side_effect=err1):
        res1 = _forward_http("http://127.0.0.1:8700/mcp", {"jsonrpc": "2.0", "id": 7})

    assert res1 == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32600, "message": "Invalid Request"},
    }

    plain_err = _make_http_error(500, "Internal Server Error", b"Gateway crash")
    with patch("urllib.request.urlopen", side_effect=plain_err):
        res2 = _forward_http("http://127.0.0.1:8700/mcp", {"jsonrpc": "2.0", "id": 8})

    assert isinstance(res2, dict)
    assert res2["id"] == 8
    assert res2["error"]["code"] == -32603
    assert "HTTP 500 Internal Server Error" in res2["error"]["message"]


def test_forward_http_supports_batch_list_response() -> None:
    """_forward_http must return batch list responses rather than dropping them."""
    batch_payload = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
    ]
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = json.dumps(batch_payload).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        res = _forward_http("http://127.0.0.1:8700/mcp", [{"jsonrpc": "2.0", "id": 1}])

    assert res == batch_payload


async def test_run_stdio_no_duplicate_execution_on_timeout_or_http_error_and_fallback_on_refused(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_stdio must NOT fall back to fallback_daemon on TimeoutError or HTTPError,
    must emit -32700 on invalid JSON, and MUST fall back on ConnectionRefusedError."""
    fallback_calls: list[dict[str, Any]] = []

    async def fake_handle_mcp(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any] | None:
        fallback_calls.append(payload)
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"fallback": True}}

    call_idx = 0

    def fake_urlopen(req: Any, timeout: float = 0.0) -> Any:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            raise TimeoutError("HTTP timed out after connecting")
        if call_idx == 2:
            raise _make_http_error(502, "Bad Gateway", b"upstream error")
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    stdin_lines = "\n".join(
        [
            "{not valid json",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        ]
    )
    fake_stdin = io.StringIO(stdin_lines + "\n")
    fake_stdout = io.StringIO()

    monkeypatch.setattr("sys.stdin", fake_stdin)
    monkeypatch.setattr("sys.stdout", fake_stdout)
    monkeypatch.setattr("conclave.mcp.__main__.conclave_home", lambda: tmp_path)
    monkeypatch.setattr("conclave.mcp.__main__.make_provider", _StubProvider)
    monkeypatch.setattr("conclave.mcp.__main__.handle_mcp_message", fake_handle_mcp)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await _run_stdio()

    out_lines = [json.loads(line) for line in fake_stdout.getvalue().splitlines() if line.strip()]
    assert len(out_lines) == 4

    # 1. Invalid JSON line -> -32700 Parse error
    assert out_lines[0]["error"]["code"] == -32700
    assert out_lines[0]["id"] is None

    # 2. TimeoutError -> -32603 error, NOT sent to fallback_daemon
    assert out_lines[1]["id"] == 1
    assert out_lines[1]["error"]["code"] == -32603

    # 3. HTTPError -> -32603 error, NOT sent to fallback_daemon
    assert out_lines[2]["id"] == 2
    assert out_lines[2]["error"]["code"] == -32603

    # 4. ConnectionRefusedError -> handled cleanly by fallback_daemon
    assert out_lines[3] == {"jsonrpc": "2.0", "id": 3, "result": {"fallback": True}}
    assert len(fallback_calls) == 1
    assert fallback_calls[0]["id"] == 3


async def test_mcp_resource_templates_and_task_resource_read(
    db: Database, tmp_path: Path
) -> None:
    """resources/templates/list returns project & task templates; resources/read serves
    conclave://tasks/{task_id} with task, checkpoints, and verdicts JSON."""
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    project = await repo.create_project(db, name="proj", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(
        db,
        project_id=project.id,
        title="Test task resource",
        request="Verify task resource read",
    )
    await repo.save_task_checkpoint(
        db,
        task_id=task.id,
        node_id="developer",
        attempt=1,
        status=CheckpointStatus.completed,
        git_tree_sha="abc1234",
        artifact={"summary": "implemented"},
    )
    await repo.add_verdict(
        db,
        task_id=task.id,
        attempt=1,
        agent="reviewer",
        verdict="pass",
        reason="looks good",
    )

    tpl_res = await handle_mcp_message(
        daemon,
        {"jsonrpc": "2.0", "id": 10, "method": "resources/templates/list"},
    )
    assert tpl_res is not None
    assert tpl_res["result"] == {
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
    }

    read_res = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "resources/read",
            "params": {"uri": f"conclave://tasks/{task.id}"},
        },
    )
    assert read_res is not None
    contents = read_res["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == f"conclave://tasks/{task.id}"
    assert contents[0]["mimeType"] == "application/json"
    body = json.loads(contents[0]["text"])
    assert body["task"]["id"] == task.id
    assert len(body["checkpoints"]) == 1
    assert body["checkpoints"][0]["node_id"] == "developer"
    assert len(body["verdicts"]) == 1
    assert body["verdicts"][0]["verdict"] == "pass"

    missing_res = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "resources/read",
            "params": {"uri": "conclave://tasks/nonexistent"},
        },
    )
    assert missing_res is not None
    assert missing_res["error"]["code"] == -32602


async def test_mcp_notifications_without_id_always_return_none(
    db: Database, tmp_path: Path
) -> None:
    """Any JSON-RPC message without 'id' in payload must return None."""
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)

    for method in ("initialized", "notifications/initialized", "notifications/cancelled", "custom"):
        res = await handle_mcp_message(daemon, {"jsonrpc": "2.0", "method": method})
        assert res is None


async def test_observability_tools_do_not_pollute_interaction_logs_and_project_id_resolution(
    db: Database, tmp_path: Path
) -> None:
    """Read-only observability tools must not record interaction_logs or emit interaction.logged;
    task-scoped tools resolve project_id from task_id, and project_id='global' maps to None."""
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    project = await repo.create_project(db, name="obs", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(
        db,
        project_id=project.id,
        title="Task for pid resolution",
        request="Test",
    )

    logged_events: list[Any] = []
    q = daemon.bus.subscribe()

    # 1. Call read-only observability tools
    for tool_name, args in [
        ("conclave_list_interactions", {}),
        ("conclave_query_interactions", {"sql": "SELECT COUNT(*) AS n FROM interaction_logs"}),
        ("conclave_get_observability_stats", {}),
        ("conclave_get_reload_status", {}),
        ("conclave_get_interaction", {"session_id": "missing-session"}),
    ]:
        await handle_mcp_message(
            daemon,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            },
        )

    rows = await repo.list_interaction_logs(db, limit=50)
    assert rows == []

    while not q.queue.empty():
        ev = q.queue.get_nowait()
        if ev.type == EventType.interaction_logged:
            logged_events.append(ev)
    assert logged_events == []

    # 2. Call tool with task_id but no project_id -> resolves project_id from task
    await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "conclave_get_task", "arguments": {"task_id": task.id}},
        },
    )
    rows_after_task = await repo.list_interaction_logs(db, limit=50)
    assert len(rows_after_task) == 1
    assert rows_after_task[0].task_id == task.id
    assert rows_after_task[0].project_id == project.id

    # 3. Call tool with project_id="global" -> stores None in project_id column
    await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "conclave_get_config", "arguments": {"project_id": "global"}},
        },
    )
    rows_after_global = await repo.list_interaction_logs(db, limit=50)
    assert len(rows_after_global) == 2
    latest = next(r for r in rows_after_global if r.title == "MCP: conclave_get_config")
    assert latest.project_id is None


async def test_mcp_tool_type_error_returns_clean_is_error_result(
    db: Database, tmp_path: Path
) -> None:
    """TypeError during tool execution must return isError=True with 'Error: ...' message."""
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)

    with patch(
        "conclave.mcp.server.call_mcp_tool",
        side_effect=TypeError("expected str, got list"),
    ):
        res = await handle_mcp_message(
            daemon,
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "conclave_list_projects", "arguments": {"limit": [1, 2]}},
            },
        )

    assert res is not None
    assert res["id"] == 99
    assert res["result"]["isError"] is True
    assert res["result"]["content"][0]["text"] == "Error: expected str, got list"
