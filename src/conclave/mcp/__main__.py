"""Stdio MCP entrypoint (`python -m conclave.mcp` or `conclave-mcp`).

Forwards JSON-RPC 2.0 MCP messages over stdin/stdout to the running Conclave daemon
(`CONCLAVE_URL`, default `http://127.0.0.1:8700`). The bridge never opens Conclave's
database or runs a tool itself: only the daemon, which holds the data-dir lock, does. While
the daemon is unreachable (stopped, or restarting during a deploy), `initialize`, `ping`,
`tools/list` and the pilot guide resource are answered from the same packaged definitions
the daemon serves, and a tool call (or any other request) gets an error naming the URL and
the reason. Every message tries the daemon first, so the bridge recovers once it is back.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .server import handle_offline_mcp_message


def _forward_http(
    mcp_url_or_payload: str | dict[str, Any] | list[Any],
    message: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any] | list[Any] | None:
    if isinstance(mcp_url_or_payload, str):
        mcp_url = mcp_url_or_payload
        payload: dict[str, Any] | list[Any] = message if message is not None else {}
    else:
        base_url = os.environ.get("CONCLAVE_URL", "http://127.0.0.1:8700").rstrip("/")
        mcp_url = f"{base_url}/mcp"
        payload = mcp_url_or_payload

    msg_id = payload.get("id") if isinstance(payload, dict) else None
    try:
        timeout = float(os.environ.get("CONCLAVE_MCP_TIMEOUT", "300.0"))
    except ValueError:
        timeout = 300.0

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        mcp_url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 202 or resp.status == 204:
                return None
            raw = resp.read().decode("utf-8").strip()
            if not raw:
                return None
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, (dict, list)) else None
    except urllib.error.HTTPError as exc:
        try:
            raw_err = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            raw_err = ""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        if raw_err:
            try:
                parsed_err = json.loads(raw_err)
                if isinstance(parsed_err, list):
                    return parsed_err
                if isinstance(parsed_err, dict) and (
                    "jsonrpc" in parsed_err or "result" in parsed_err or "error" in parsed_err
                ):
                    return parsed_err
            except json.JSONDecodeError:
                pass
        detail = f": {raw_err}" if raw_err else ""
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32603,
                "message": f"HTTP {exc.code} {exc.reason}{detail}",
            },
        }
    except TimeoutError as exc:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32603,
                "message": f"MCP HTTP request timed out after {timeout}s: {exc}",
            },
        }
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": f"MCP HTTP request timed out after {timeout}s: {exc.reason}",
                },
            }
        raise


def _answer_offline(
    msg: dict[str, Any] | list[Any], unreachable: str
) -> dict[str, Any] | list[Any] | None:
    """Answer *msg* (one message or a batch) without the daemon; ``None``: nothing to send."""
    if isinstance(msg, list):
        replies: list[dict[str, Any]] = []
        for item in msg:
            if isinstance(item, dict):
                reply = handle_offline_mcp_message(item, unreachable)
                if reply is not None:
                    replies.append(reply)
        return replies if replies else None
    return handle_offline_mcp_message(msg, unreachable)


async def _run_stdio() -> None:
    base_url = os.environ.get("CONCLAVE_URL", "http://127.0.0.1:8700").rstrip("/")
    mcp_url = f"{base_url}/mcp"

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc.msg}"},
            }
            sys.stdout.write(json.dumps(parse_err) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(msg, (dict, list)):
            inv_err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
            sys.stdout.write(json.dumps(inv_err) + "\n")
            sys.stdout.flush()
            continue

        response: dict[str, Any] | list[Any] | None = None
        try:
            response = await asyncio.to_thread(_forward_http, mcp_url, msg)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:
                pass
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"HTTP {exc.code} {exc.reason}"},
            }
        except TimeoutError as exc:
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"MCP HTTP request timed out: {exc}"},
            }
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32603,
                        "message": f"MCP HTTP request timed out: {exc.reason}",
                    },
                }
            else:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                unreachable = (
                    f"Conclave daemon unreachable at {base_url}: "
                    f"{str(reason) or type(reason).__name__}. "
                    "Start it (./conclave or systemctl --user start conclave) and retry."
                )
                response = _answer_offline(msg, unreachable)

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
