"""Unit tests for MCP stdio proxy reliability and MCP JSON-RPC protocol completeness."""

from __future__ import annotations

import asyncio
import http.client
import http.server
import io
import json
import socket
import threading
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
from conclave.mcp.server import handle_mcp_message, handle_offline_mcp_message
from conclave.mcp.tools import MCP_TOOLS, PILOT_GUIDE_URI, load_pilot_brief, load_pilot_guide
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


def _reply(read: Any) -> MagicMock:
    """A fake 200 response whose ``read`` is *read* (a side effect: a callable or exception)."""
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.side_effect = read
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    return fake_resp


def test_forward_http_turns_a_reply_that_is_not_complete_json_into_an_error() -> None:
    """A reply that is not complete JSON (a proxy's HTML page, or a body cut short because the
    daemon died mid-reply) is a -32603 error for that request, never an exception: before the
    fix the JSONDecodeError or IncompleteRead escaped and ended the bridge."""
    cases = [
        (9, _reply(lambda *_: b"<html>502 Bad Gateway</html>")),
        (10, _reply(lambda *_: b'{"jsonrpc": "2.0", "id": 10, "res')),
        (11, _reply(http.client.IncompleteRead(b'{"jsonrpc": "2.0", "id": 11'))),
    ]
    for msg_id, fake_resp in cases:
        with patch("urllib.request.urlopen", return_value=fake_resp):
            res = _forward_http(
                "http://127.0.0.1:8700/mcp",
                {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call"},
            )
        assert isinstance(res, dict), msg_id
        assert res["id"] == msg_id
        assert res["error"]["code"] == -32603


async def test_run_stdio_keeps_answering_after_a_reply_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable reply does not end the bridge: the next message is still forwarded."""
    replies = iter(
        [
            _reply(lambda *_: b"<html>oops</html>"),
            _reply(lambda *_: json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}).encode()),
        ]
    )
    messages = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in (1, 2)]
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
    monkeypatch.setattr("sys.stdout", fake_stdout)
    with patch("urllib.request.urlopen", side_effect=lambda *_a, **_k: next(replies)):
        await _run_stdio()

    out = [json.loads(line) for line in fake_stdout.getvalue().splitlines() if line.strip()]
    assert [line["id"] for line in out] == [1, 2]
    assert out[0]["error"]["code"] == -32603
    assert out[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


async def test_run_stdio_no_duplicate_execution_on_timeout_or_http_error_and_offline_on_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_stdio must answer TimeoutError and HTTPError with -32603 (never anything that could
    run the call again), emit -32700 on invalid JSON, and answer locally, without a database,
    on ConnectionRefusedError."""
    home = tmp_path / "home"
    monkeypatch.setenv("CONCLAVE_HOME", str(home))
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

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await _run_stdio()

    out_lines = [json.loads(line) for line in fake_stdout.getvalue().splitlines() if line.strip()]
    assert len(out_lines) == 4

    # 1. Invalid JSON line -> -32700 Parse error
    assert out_lines[0]["error"]["code"] == -32700
    assert out_lines[0]["id"] is None

    # 2. TimeoutError -> -32603 error, not answered as if the daemon were down
    assert out_lines[1]["id"] == 1
    assert out_lines[1]["error"]["code"] == -32603

    # 3. HTTPError -> -32603 error, not answered as if the daemon were down
    assert out_lines[2]["id"] == 2
    assert out_lines[2]["error"]["code"] == -32603

    # 4. ConnectionRefusedError -> answered locally, without opening any database
    assert out_lines[3] == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert call_idx == 3
    assert not home.exists()


async def test_run_stdio_never_answers_a_notification_even_when_the_daemon_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON-RPC forbids answering a notification: when the daemon errors, times out or sends an
    unreadable reply, a notification (or a batch of them) gets nothing, a request its error."""
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    cancelled = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}
    script: list[Any] = [
        # The five notifications first, each meeting another failure...
        _make_http_error(502, "Bad Gateway", b"upstream error"),
        TimeoutError("timed out"),
        urllib.error.URLError(TimeoutError("socket timed out")),
        _reply(lambda *_: b"<html>oops</html>"),
        _make_http_error(500, "Internal Server Error", b""),
        # ... then two requests, which still get the bridge's error.
        _make_http_error(502, "Bad Gateway", b"upstream error"),
        TimeoutError("timed out"),
    ]
    messages: list[Any] = [
        notification, notification, notification, notification, [notification, cancelled],
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]

    def fake_urlopen(req: Any, timeout: float = 0.0) -> Any:
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
    monkeypatch.setattr("sys.stdout", fake_stdout)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await _run_stdio()

    assert script == []  # every message, notifications included, reached the daemon
    out = [json.loads(line) for line in fake_stdout.getvalue().splitlines() if line.strip()]
    assert [(line["id"], line["error"]["code"]) for line in out] == [(1, -32603), (2, -32603)]


_RETRY_HINT = "Start it (./conclave or systemctl --user start conclave) and retry."


def _closed_local_port() -> int:
    """A local TCP port nothing listens on (bound, read, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


async def _run_stdio_lines(
    monkeypatch: pytest.MonkeyPatch, url: str, home: Path, messages: list[Any]
) -> list[Any]:
    """Run the stdio bridge against *url* on *messages* and return the parsed output lines."""
    monkeypatch.setenv("CONCLAVE_URL", url)
    monkeypatch.setenv("CONCLAVE_HOME", str(home))
    monkeypatch.setenv("CONCLAVE_MCP_TIMEOUT", "10")
    # urllib honours *_proxy variables: talk to 127.0.0.1 directly.
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
    monkeypatch.setattr("sys.stdout", fake_stdout)
    await _run_stdio()
    return [json.loads(line) for line in fake_stdout.getvalue().splitlines() if line.strip()]


async def test_run_stdio_with_the_daemon_down_answers_locally_and_never_opens_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing listening at CONCLAVE_URL, the bridge answers initialize, ping, tools/list
    and the guide resource itself, refuses every tool call (mutating ones included) with an
    isError result, and never creates or opens a database in CONCLAVE_HOME."""

    def no_database(self: Database) -> None:
        raise AssertionError("the stdio bridge opened a database")

    monkeypatch.setattr(Database, "connect", no_database)
    port = _closed_local_port()
    home = tmp_path / "home"
    merge = {"name": "conclave_merge_task", "arguments": {"task_id": "t1"}}
    delete = {"name": "conclave_delete_task", "arguments": {"task_id": "t1"}}
    out = await _run_stdio_lines(
        monkeypatch,
        f"http://127.0.0.1:{port}/",
        home,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": PILOT_GUIDE_URI},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": merge},
            [
                {"jsonrpc": "2.0", "id": 7, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/cancelled"},
                {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": delete},
            ],
            [{"jsonrpc": "2.0", "method": "notifications/initialized"}],
        ],
    )

    assert len(out) == 7  # one line per request or batch with a request; none for notifications
    init = out[0]
    assert init["id"] == 1
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "conclave-mcp"
    assert init["result"]["instructions"] == load_pilot_brief()
    assert out[1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": json.loads(json.dumps(MCP_TOOLS))},
    }
    assert [r["uri"] for r in out[2]["result"]["resources"]] == [PILOT_GUIDE_URI]
    assert out[3]["result"]["contents"][0]["text"] == load_pilot_guide()
    assert out[4] == {"jsonrpc": "2.0", "id": 5, "result": {}}

    call = out[5]
    assert call["id"] == 6
    assert call["result"]["isError"] is True
    text = call["result"]["content"][0]["text"]
    assert text.startswith(f"Conclave daemon unreachable at http://127.0.0.1:{port}: ")
    assert "refused" in text.lower()
    assert text.endswith(_RETRY_HINT)

    batch = out[6]
    assert isinstance(batch, list) and len(batch) == 2
    assert batch[0] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert batch[1]["id"] == 8
    assert batch[1]["result"] == {"content": [{"type": "text", "text": text}], "isError": True}

    assert not (home / "conclave.db").exists()
    assert not home.exists()


def _recording_mcp_handler(
    received: list[tuple[str, str | None, Any]],
) -> type[http.server.BaseHTTPRequestHandler]:
    """A stand-in daemon: records each POST and answers requests with 200, notifications 202."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            received.append((self.path, self.headers.get("Content-Type"), body))
            if "id" not in body:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            reply = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "merged by the daemon"}],
                        "isError": False,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


async def test_run_stdio_forwards_to_a_reachable_daemon_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable daemon gets every message verbatim at POST /mcp; its reply is written back
    as it came, a notification's 202 writes nothing, and the bridge needs no CONCLAVE_HOME."""
    received: list[tuple[str, str | None, Any]] = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _recording_mcp_handler(received))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    call = {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {"name": "conclave_merge_task", "arguments": {"task_id": "t1"}},
    }
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        port = server.server_address[1]
        out = await _run_stdio_lines(
            monkeypatch, f"http://127.0.0.1:{port}", home, [call, notification]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert received == [
        ("/mcp", "application/json", call),
        ("/mcp", "application/json", notification),
    ]
    assert out == [
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "result": {
                "content": [{"type": "text", "text": "merged by the daemon"}],
                "isError": False,
            },
        }
    ]
    assert not home.exists()


async def test_offline_answers_match_the_daemon_and_refuse_what_needs_it(
    db: Database, tmp_path: Path
) -> None:
    """The bridge's offline answers are the daemon's own for every request that needs no daemon
    state; a tool call is an isError result and anything else a -32603 error naming why."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    unreachable = (
        "Conclave daemon unreachable at http://127.0.0.1:9: [Errno 111] Connection refused. "
        + _RETRY_HINT
    )
    for message in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": PILOT_GUIDE_URI}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": None, "method": "notifications/cancelled"},
    ):
        expected = await handle_mcp_message(daemon, message)
        assert handle_offline_mcp_message(message, unreachable) == expected, message

    task_uri = {"uri": "conclave://tasks/x"}
    for message in (
        {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": task_uri},
        {"jsonrpc": "2.0", "id": 8, "method": "resources/templates/list"},
        {"jsonrpc": "2.0", "id": 9, "method": "no/such/method"},
    ):
        assert handle_offline_mcp_message(message, unreachable) == {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32603, "message": unreachable},
        }

    call = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "conclave_list_projects", "arguments": {}},
    }
    assert handle_offline_mcp_message(call, unreachable) == {
        "jsonrpc": "2.0",
        "id": 10,
        "result": {"content": [{"type": "text", "text": unreachable}], "isError": True},
    }


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
