"""Hidden acceptance tests for W10 (opencode worker isolation).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. A fake ``opencode`` answers ``debug config`` with two MCP servers
and records the environment of every ``run``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers.profiles import ResolvedProfile

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX fake executable")

_CONFIG = {"mcp": {"playwright": {"type": "local", "command": ["npx"]},
                   "docs": {"type": "remote", "url": "http://x.invalid"}}}
_OK_STREAM = [
    {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "done"}},
    {"type": "step_finish", "sessionID": "ses_1",
     "part": {"type": "step-finish", "tokens": {"input": 1, "output": 1}, "cost": 0}},
]


def _fake_opencode(tmp_path: Path, *, debug_exit: int = 0, debug_out: str | None = None) -> Path:
    log = tmp_path / "calls.jsonl"
    out = json.dumps(_CONFIG) if debug_out is None else debug_out
    stream = "\n".join(json.dumps(x) for x in _OK_STREAM)
    script = tmp_path / "opencode"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        f"log = open({str(log)!r}, 'a')\n"
        "kind = 'debug' if args[:2] == ['debug', 'config'] else 'run'\n"
        "log.write(json.dumps({'kind': kind, 'args': args, 'env': dict(os.environ)}) + '\\n')\n"
        "log.close()\n"
        "if kind == 'debug':\n"
        f"    sys.stdout.write({out!r}); sys.exit({debug_exit})\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({stream!r} + '\\n')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def _dispatch(
    tmp_path: Path,
    monkeypatch: Any,
    cwd: Path,
    *,
    isolated: bool,
    extra_env: dict[str, str] | None = None,
) -> Any:
    from conclave.providers import opencode_cli

    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(tmp_path / "opencode"))
    profile = ResolvedProfile(
        name="system-default", arg_mode=ArgMode.inherit, isolated=isolated,
        extra_env=extra_env or {},
    )
    return await opencode_cli.OpenCodeCliProvider().run_agent(
        profile=profile, prompt="x", timeout_seconds=30, cwd=cwd
    )


def _run_env(tmp_path: Path) -> dict[str, str]:
    runs = [c for c in _calls(tmp_path) if c["kind"] == "run"]
    assert runs, "opencode run was never invoked"
    return runs[-1]["env"]


async def test_isolated_disables_skills_and_every_discovered_mcp(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    _fake_opencode(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    res = await _dispatch(tmp_path, monkeypatch, wt, isolated=True)
    assert res.ok, res.error
    env = _run_env(tmp_path)
    assert env.get("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS") == "1"
    assert env.get("OPENCODE_DISABLE_EXTERNAL_SKILLS") == "1"
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert content["mcp"] == {"playwright": {"enabled": False}, "docs": {"enabled": False}}


async def test_not_isolated_keeps_environment(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", raising=False)
    monkeypatch.delenv("OPENCODE_DISABLE_EXTERNAL_SKILLS", raising=False)
    _fake_opencode(tmp_path)
    res = await _dispatch(
        tmp_path, monkeypatch, tmp_path, isolated=False, extra_env={"FOO_W10": "bar"}
    )
    assert res.ok, res.error
    env = _run_env(tmp_path)
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS" not in env
    assert "OPENCODE_DISABLE_EXTERNAL_SKILLS" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert env.get("FOO_W10") == "bar", "profile.extra_env must reach opencode"
    debug_calls = [c for c in _calls(tmp_path) if c["kind"] == "debug"]
    assert not debug_calls, "no discovery when not isolated"


async def test_operator_config_content_is_merged(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps({"model": "x/y", "mcp": {"playwright": {"enabled": True, "timeout": 5}}}),
    )
    _fake_opencode(tmp_path)
    res = await _dispatch(tmp_path, monkeypatch, tmp_path, isolated=True)
    assert res.ok, res.error
    content = json.loads(_run_env(tmp_path)["OPENCODE_CONFIG_CONTENT"])
    assert content["model"] == "x/y"
    assert content["mcp"]["playwright"]["enabled"] is False
    assert content["mcp"]["docs"] == {"enabled": False}


async def test_extra_env_beats_isolation(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_opencode(tmp_path)
    res = await _dispatch(
        tmp_path, monkeypatch, tmp_path, isolated=True,
        extra_env={"OPENCODE_DISABLE_EXTERNAL_SKILLS": "0"},
    )
    assert res.ok, res.error
    assert _run_env(tmp_path)["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "0"


@pytest.mark.parametrize(("code", "out"), [(1, ""), (0, "not json at all")])
async def test_discovery_failure_still_dispatches(
    tmp_path: Path, monkeypatch: Any, code: int, out: str
) -> None:
    _fake_opencode(tmp_path, debug_exit=code, debug_out=out)
    res = await _dispatch(tmp_path, monkeypatch, tmp_path, isolated=True)
    assert res.ok, res.error
    env = _run_env(tmp_path)
    assert env.get("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS") == "1"
    assert env.get("OPENCODE_DISABLE_EXTERNAL_SKILLS") == "1"


async def test_discovery_cached_per_cwd(tmp_path: Path, monkeypatch: Any) -> None:
    from conclave.providers import opencode_cli

    _fake_opencode(tmp_path)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(tmp_path / "opencode"))
    provider = opencode_cli.OpenCodeCliProvider()
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    profile = ResolvedProfile(name="system-default", arg_mode=ArgMode.inherit, isolated=True)
    for cwd in (a, a, b):
        res = await provider.run_agent(profile=profile, prompt="x", timeout_seconds=30, cwd=cwd)
        assert res.ok, res.error
    debug_calls = [c for c in _calls(tmp_path) if c["kind"] == "debug"]
    assert len(debug_calls) == 2
