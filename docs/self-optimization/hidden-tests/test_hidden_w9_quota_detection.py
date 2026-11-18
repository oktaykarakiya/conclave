"""Hidden acceptance tests for W9 (usage-limit detection in the providers).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Fake ``claude`` / ``opencode`` executables replay the stream
formats measured on Claude Code 2.1.326 and opencode 1.19.5.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers.profiles import ResolvedProfile

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX fake executables")

RESET = 1794924600


def _fake_cli(tmp_path: Path, name: str, lines: list[dict[str, Any] | str], exit_code: int) -> Path:
    """An executable that drains stdin, prints *lines*, counts its `run`s, and exits."""
    payload = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines)
    counter = tmp_path / f"{name}.runs"
    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['debug', 'config']:\n"
        "    print('{}'); sys.exit(0)\n"
        "sys.stdin.read()\n"
        f"open({str(counter)!r}, 'a').write('x')\n"
        f"sys.stdout.write({payload!r} + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _runs(tmp_path: Path, name: str) -> int:
    counter = tmp_path / f"{name}.runs"
    return len(counter.read_text()) if counter.exists() else 0


async def _run_claude(tmp_path: Path, lines: list[dict[str, Any] | str], exit_code: int) -> Any:
    from conclave.providers.claude_cli import ClaudeCliProvider

    cli = _fake_cli(tmp_path, "claude", lines, exit_code)
    profile = ResolvedProfile(name="claude-code", arg_mode=ArgMode.flag, cli=str(cli))
    return await ClaudeCliProvider().run_agent(
        profile=profile, prompt="x", timeout_seconds=30, cwd=tmp_path
    )


def _init() -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "m"}


def _rl(status: str, util: float = 0.5) -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "resetsAt": RESET,
            "rateLimitType": "five_hour",
            "unifiedWindows": {
                "five_hour": {"utilization": util, "resetsAt": RESET, "status": status},
                "seven_day": {"utilization": 0.3, "resetsAt": RESET + 86400, "status": "allowed"},
            },
        },
        "session_id": "sess-1",
    }


def _assistant(text: str, error: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "session_id": "sess-1",
    }
    if error:
        msg["error"] = error
    return msg


def _result(text: str, is_error: bool) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": text,
        "session_id": "sess-1",
    }


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_claude_rejected_rate_limit_is_quota_not_ok(tmp_path: Path, exit_code: int) -> None:
    text = "You've hit your limit \u00b7 resets 3:10pm"
    res = await _run_claude(
        tmp_path,
        [_init(), _rl("rejected", 1.0), _assistant(text, "rate_limit"), _result(text, False)],
        exit_code,
    )
    assert res.ok is False
    assert res.quota is not None
    assert res.quota.kind == "rate_limit"
    assert res.quota.resets_at == datetime.fromtimestamp(RESET, UTC)
    assert res.session_id == "sess-1"


async def test_claude_assistant_rate_limit_error_takes_reset_from_info(tmp_path: Path) -> None:
    text = "usage limit reached \u00b7 continues automatically when it resets"
    res = await _run_claude(
        tmp_path,
        [
            _init(),
            _rl("allowed_warning", 0.97),
            _assistant(text, "rate_limit"),
            _result(text, True),
        ],
        1,
    )
    assert res.ok is False
    assert res.quota is not None and res.quota.kind == "rate_limit"
    assert res.quota.resets_at == datetime.fromtimestamp(RESET, UTC)


async def test_claude_billing_error(tmp_path: Path) -> None:
    text = "Credit balance is too low"
    res = await _run_claude(
        tmp_path, [_init(), _assistant(text, "billing_error"), _result(text, True)], 1
    )
    assert res.ok is False
    assert res.quota is not None and res.quota.kind == "billing"


async def test_claude_legacy_text_with_epoch(tmp_path: Path) -> None:
    res = await _run_claude(
        tmp_path, [_result(f"Claude AI usage limit reached|{RESET}", True)], 1
    )
    assert res.ok is False
    assert res.quota is not None and res.quota.kind == "rate_limit"
    assert res.quota.resets_at == datetime.fromtimestamp(RESET, UTC)


async def test_claude_normal_run_with_warning_has_no_quota(tmp_path: Path) -> None:
    res = await _run_claude(
        tmp_path,
        [_init(), _rl("allowed"), _rl("allowed_warning", 0.9), _assistant("done"),
         _result("done", False)],
        0,
    )
    assert res.ok is True
    assert res.quota is None


def _oc_error(status: int, message: str, headers: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "error",
        "timestamp": 1794908738998,
        "sessionID": "ses_abc",
        "error": {
            "name": "APIError",
            "data": {
                "message": message,
                "statusCode": status,
                "isRetryable": status == 429,
                "responseHeaders": headers,
            },
        },
    }


async def _run_opencode(tmp_path: Path, monkeypatch: Any, event: dict[str, Any]) -> Any:
    from conclave.providers import opencode_cli

    cli = _fake_cli(tmp_path, "opencode", [event], 1)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(cli))
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0, raising=False)
    profile = ResolvedProfile(name="system-default", arg_mode=ArgMode.inherit,
                              model="deepseek/deepseek-flash")
    return await opencode_cli.OpenCodeCliProvider().run_agent(
        profile=profile, prompt="x", timeout_seconds=30, cwd=tmp_path
    )


async def test_opencode_402_is_billing_quota_without_retry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    res = await _run_opencode(
        tmp_path, monkeypatch,
        _oc_error(
            402,
            "Insufficient Balance (request_id: 9b73)",
            {"content-type": "application/json"},
        ),
    )
    assert res.ok is False
    assert res.quota is not None and res.quota.kind == "billing"
    assert res.quota.resets_at is None
    assert _runs(tmp_path, "opencode") == 1


@pytest.mark.parametrize(
    ("headers", "seconds"), [({"retry-after": "30"}, 30), ({"retry-after-ms": "45000"}, 45)]
)
async def test_opencode_429_uses_retry_after(
    tmp_path: Path, monkeypatch: Any, headers: dict[str, str], seconds: int
) -> None:
    before = datetime.now(UTC)
    res = await _run_opencode(tmp_path, monkeypatch, _oc_error(429, "Rate limit exceeded", headers))
    after = datetime.now(UTC)
    assert res.ok is False
    assert res.quota is not None and res.quota.kind == "rate_limit"
    assert res.quota.resets_at is not None
    assert before + timedelta(seconds=seconds - 1) <= res.quota.resets_at
    assert res.quota.resets_at <= after + timedelta(seconds=seconds + 1)
    assert _runs(tmp_path, "opencode") == 1
