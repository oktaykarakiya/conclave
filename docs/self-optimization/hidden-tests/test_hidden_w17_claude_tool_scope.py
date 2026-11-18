"""Hidden acceptance tests for W17 (isolated Claude Code worker tool scope, usage summing).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. A fake `claude` executable records its argv and environment.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from conclave.config import ArgMode
from conclave.providers.claude_cli import ClaudeCliProvider
from conclave.providers.profiles import ResolvedProfile, build_invocation

_OUT_OF_SCOPE = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
    "ScheduleWakeup",
    "PushNotification",
    "SendMessage",
    "ListAgents",
    "Workflow",
    "EnterWorktree",
    "ExitWorktree",
    "DesignSync",
)
_CORE = ("Bash", "Read", "Edit", "Write")


def _result(turns: int, out_tokens: int, cost: float, text: str) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": "s-1",
        "num_turns": turns,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 10,
            "output_tokens": out_tokens,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 100,
        },
    }


def _fake_claude(tmp: Path, results: list[dict[str, object]]) -> Path:
    record = tmp / "record.json"
    script = tmp / "fake_claude.py"
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"})]
    lines += [json.dumps(r) for r in results]
    body = "\n".join(lines) + "\n"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
        "'env': dict(os.environ)}))\n"
        f"sys.stdout.write({body!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _tools_arg(args: list[str]) -> list[str]:
    assert args.count("--tools") == 1, args
    raw = args[args.index("--tools") + 1]
    return [t for t in raw.replace(",", " ").split() if t]


def test_isolated_invocation_has_tool_allowlist_and_switches() -> None:
    inv = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=True))
    tools = _tools_arg(inv.args)
    for core in _CORE:
        assert core in tools, tools
    for banned in _OUT_OF_SCOPE:
        assert banned not in tools, banned
    assert "--strict-mcp-config" in inv.args  # W8 isolation is kept
    assert inv.env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "1"
    assert inv.env.get("CLAUDE_CODE_DISABLE_CRON") == "1"


def test_not_isolated_has_no_scope_changes() -> None:
    inv = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=False))
    assert "--tools" not in inv.args
    for key in ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_CRON"):
        assert key not in inv.env


def test_extra_env_still_wins() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="p",
            arg_mode=ArgMode.flag,
            isolated=True,
            extra_env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"},
        )
    )
    assert inv.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "0"


async def _run(tmp: Path, *, isolated: bool, timeout: int,
               results: list[dict[str, object]]) -> tuple[dict[str, object], object]:
    script = _fake_claude(tmp, results)
    profile = ResolvedProfile(name="p", arg_mode=ArgMode.flag, cli=str(script), isolated=isolated)
    res = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="do it", timeout_seconds=timeout, cwd=tmp
    )
    record = json.loads((tmp / "record.json").read_text(encoding="utf-8"))
    return record, res


async def test_provider_sets_bash_timeouts_from_dispatch_timeout(tmp_path: Path) -> None:
    one = [_result(3, 50, 0.1, "VERDICT: done")]
    record, _ = await _run(tmp_path, isolated=True, timeout=1800, results=one)
    env = record["env"]
    assert isinstance(env, dict)
    assert env.get("BASH_MAX_TIMEOUT_MS") == "1800000"
    assert env.get("BASH_DEFAULT_TIMEOUT_MS") == "1200000"
    assert env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "1"
    argv = record["argv"]
    assert isinstance(argv, list) and "--tools" in argv

    short = tmp_path / "short"
    short.mkdir()
    record, _ = await _run(short, isolated=True, timeout=600, results=one)
    env = record["env"]
    assert isinstance(env, dict)
    assert env.get("BASH_MAX_TIMEOUT_MS") == "600000"
    assert env.get("BASH_DEFAULT_TIMEOUT_MS") == "600000"


async def test_provider_not_isolated_sets_nothing(tmp_path: Path) -> None:
    saved = {k: os.environ.pop(k) for k in ("BASH_MAX_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS",
                                            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
                                            "CLAUDE_CODE_DISABLE_CRON") if k in os.environ}
    try:
        record, _ = await _run(tmp_path, isolated=False, timeout=1800,
                               results=[_result(3, 50, 0.1, "VERDICT: done")])
    finally:
        os.environ.update(saved)
    env = record["env"]
    assert isinstance(env, dict)
    for key in ("BASH_MAX_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS",
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_CRON"):
        assert key not in env, key
    argv = record["argv"]
    assert isinstance(argv, list) and "--tools" not in argv


async def test_two_result_events_sum_usage(tmp_path: Path) -> None:
    results = [
        _result(102, 120_495, 8.4698472, "first answer VERDICT"),
        _result(1, 8_620, 8.8780042, "final answer VERDICT"),
    ]
    _, res = await _run(tmp_path, isolated=True, timeout=1800, results=results)
    assert res.ok, res.error
    assert res.num_turns == 103
    assert res.output_tokens == 129_115
    assert res.input_tokens == 20
    assert res.cache_read_tokens == 2000
    assert res.cache_creation_tokens == 200
    assert abs((res.cost_usd or 0) - 8.8780042) < 1e-9
    assert "final answer" in res.text


async def test_single_result_unchanged(tmp_path: Path) -> None:
    _, res = await _run(tmp_path, isolated=True, timeout=1800,
                        results=[_result(7, 500, 0.25, "VERDICT: only")])
    assert res.num_turns == 7
    assert res.output_tokens == 500
    assert abs((res.cost_usd or 0) - 0.25) < 1e-9


async def test_limit_seen_before_timeout_is_quota_not_timeout(tmp_path: Path) -> None:
    """A run that reports a rejected limit and then hangs until the timeout kills it."""
    reset = 1794924600
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-9"},
        {"type": "rate_limit_event", "session_id": "s-9", "rate_limit_info": {
            "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": reset,
                                             "status": "rejected"}}}},
    ]
    body = "\n".join(json.dumps(e) for e in events) + "\n"
    script = tmp_path / "hanging_claude.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({body!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    profile = ResolvedProfile(name="p", arg_mode=ArgMode.flag, cli=str(script), isolated=True)
    res = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="x", timeout_seconds=3, cwd=tmp_path
    )
    assert not res.ok
    assert res.quota is not None, res.error
    assert res.session_id == "s-9"
    assert "timed out" not in (res.error or "")


async def test_burst_rate_limit_does_not_wait_for_the_window_reset(tmp_path: Path) -> None:
    """error: rate_limit while the account's windows are still allowed: no far-off reset."""
    far = 1794924600
    stream = [
        {"type": "system", "subtype": "init", "session_id": "s-3"},
        {"type": "rate_limit_event", "session_id": "s-3", "rate_limit_info": {
            "status": "allowed", "resetsAt": far, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": far,
                                             "status": "allowed"}}}},
        {"type": "assistant", "session_id": "s-3", "error": "rate_limit",
         "message": {"content": [{"type": "text", "text": "API Error: 429 rate limited"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "session_id": "s-3",
         "result": "API Error: 429 rate limited"},
    ]
    script = tmp_path / "burst_claude.py"
    body = "\n".join(json.dumps(e) for e in stream) + "\n"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({body!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    profile = ResolvedProfile(name="p", arg_mode=ArgMode.flag, cli=str(script), isolated=True)
    res = await ClaudeCliProvider().run_agent(profile=profile, prompt="x", timeout_seconds=30,
                                              cwd=tmp_path)
    assert res.quota is not None
    assert res.quota.resets_at is None  # the gate falls back to quota_retry_minutes
