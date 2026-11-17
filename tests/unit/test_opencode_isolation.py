"""Worker isolation of opencode dispatches (``execution.worker_isolation``) and their environment.

An isolated dispatch loads no ``.claude`` / ``.agents`` skills and disables every MCP server of the
opencode config it resolves (named by ``opencode debug config``) through
``OPENCODE_CONFIG_CONTENT``. Every dispatch gets the daemon's environment plus the profile's
``extra_env``, applied last. Each test runs a fake ``opencode`` or stubs the discovery: the real
CLI never starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import AgentResult, opencode_cli
from conclave.providers.opencode_cli import (
    OpenCodeCliProvider,
    _mcp_server_names,
    _McpDiscoveryError,
)
from conclave.providers.profiles import ResolvedProfile

_SKILL_VARS = ("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "OPENCODE_DISABLE_EXTERNAL_SKILLS")
_CONTENT = "OPENCODE_CONFIG_CONTENT"
_ISOLATION_VARS = (*_SKILL_VARS, _CONTENT)
_LOGGER = "conclave.providers.opencode_cli"
_TEXT_EVENT = {"type": "text", "part": {"type": "text", "text": "done"}}


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This suite may itself run inside an isolated opencode worker, which has these set.
    for var in _ISOLATION_VARS:
        monkeypatch.delenv(var, raising=False)
    # A test that installs no fake CLI can never start the real one.
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(tmp_path / "no-opencode"))


def _fake_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    debug_config: str = "print('{}')\n",
    run: str = f"sys.stdout.write(json.dumps({_TEXT_EVENT!r}) + '\\n')\n",
) -> str:
    """Install a fake ``opencode`` whose ``debug config`` runs *debug_config* and whose ``run``
    reads the prompt and then runs *run*; return its path."""
    script = tmp_path / "fake_opencode.py"
    indented = "".join(f"    {line}\n" for line in debug_config.splitlines())
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "if sys.argv[1:] == ['debug', 'config']:\n"
        f"{indented}"
        "    sys.exit(0)\n"
        "sys.stdin.buffer.read()\n" + run,
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(script))
    return str(script)


def _capture_spawns(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> list[tuple[list[str], dict[str, str]]]:
    """Record the CLI args (after *script*) and the ``env`` of each process started for
    *script*, then start it for real."""
    spawns: list[tuple[list[str], dict[str, str]]] = []
    spawn = asyncio.create_subprocess_exec

    async def capturing_spawn(
        program: Any, *args: Any, **kwargs: Any
    ) -> asyncio.subprocess.Process:
        argv = [str(a) for a in (program, *args)]
        if script in argv:
            spawns.append((argv[argv.index(script) + 1 :], dict(kwargs["env"])))
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_spawn)
    return spawns


def _runs(spawns: list[tuple[list[str], dict[str, str]]]) -> list[tuple[list[str], dict[str, str]]]:
    return [(args, env) for args, env in spawns if args[:1] == ["run"]]


def _stub_discovery(
    monkeypatch: pytest.MonkeyPatch, found: tuple[str, ...] | Exception
) -> list[tuple[Path | None, dict[str, str]]]:
    """Replace ``opencode debug config`` with *found* (names, or an error to raise); return the
    ``(cwd, env)`` of each discovery."""
    calls: list[tuple[Path | None, dict[str, str]]] = []

    async def discover(cwd: Path | None, env: dict[str, str]) -> tuple[str, ...]:
        calls.append((cwd, dict(env)))
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(opencode_cli, "_discover_mcp_servers", discover)
    return calls


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


async def _dispatch(
    cwd: Path,
    *,
    provider: OpenCodeCliProvider | None = None,
    isolated: bool = True,
    extra_env: dict[str, str] | None = None,
) -> AgentResult:
    profile = ResolvedProfile(
        name="opencode", arg_mode=ArgMode.inherit, isolated=isolated, extra_env=extra_env or {}
    )
    return await (provider or OpenCodeCliProvider()).run_agent(
        profile=profile, prompt="hi", timeout_seconds=30, cwd=cwd
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


# --- what an isolated dispatch gets --------------------------------------------------------------


async def test_an_isolated_dispatch_disables_skills_and_every_discovered_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    calls = _stub_discovery(monkeypatch, ("docs", "playwright"))
    cwd = _worktree(tmp_path)

    isolated = await _dispatch(cwd)
    unisolated = await _dispatch(cwd, isolated=False)

    assert isolated.ok and unisolated.ok
    [(isolated_args, env), (unisolated_args, _env)] = _runs(spawns)
    assert {var: env[var] for var in _SKILL_VARS} == dict.fromkeys(_SKILL_VARS, "1")
    assert json.loads(env[_CONTENT]) == {
        "mcp": {"docs": {"enabled": False}, "playwright": {"enabled": False}}
    }
    assert isolated_args == unisolated_args  # isolation adds no flag
    assert [call_cwd for call_cwd, _ in calls] == [cwd]


async def test_an_isolated_dispatch_without_mcp_servers_sets_no_config_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    _stub_discovery(monkeypatch, ())

    assert (await _dispatch(_worktree(tmp_path))).ok

    [(_args, env)] = _runs(spawns)
    assert {var: env[var] for var in _SKILL_VARS} == dict.fromkeys(_SKILL_VARS, "1")
    assert _CONTENT not in env


async def test_an_unisolated_dispatch_gets_the_daemon_environment_and_extra_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    calls = _stub_discovery(monkeypatch, ("playwright",))
    cwd = _worktree(tmp_path)

    result = await _dispatch(cwd, isolated=False, extra_env={"CONCLAVE_TEST_X": "1"})

    assert result.ok
    [(args, env)] = spawns
    assert calls == []  # no discovery
    assert env == {**os.environ, "CONCLAVE_TEST_X": "1"}
    assert not set(_ISOLATION_VARS) & set(env)
    assert args == ["run", "--format", "json", "--auto", "--title", "conclave", "--dir", str(cwd)]


async def test_extra_env_wins_over_the_isolation_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    calls = _stub_discovery(monkeypatch, ("playwright",))
    extra_env = {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "0", _CONTENT: '{"mcp":{}}'}

    assert (await _dispatch(_worktree(tmp_path), extra_env=extra_env)).ok

    [(_args, env)] = _runs(spawns)
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "0"
    assert env[_CONTENT] == '{"mcp":{}}'
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"  # not overridden: still applies
    # Discovery saw the dispatch's environment without the isolation overrides.
    [(_cwd, discovery_env)] = calls
    assert discovery_env == {**os.environ, **extra_env}
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS" not in discovery_env


_PLAYWRIGHT = {"type": "local", "command": ["npx", "-y", "@playwright/mcp"], "enabled": True}


@pytest.mark.parametrize(
    ("daemon_value", "expected"),
    [
        (
            json.dumps({"theme": "dark", "mcp": {"playwright": _PLAYWRIGHT}}),
            {
                "theme": "dark",
                "mcp": {
                    "docs": {"enabled": False},
                    "playwright": {**_PLAYWRIGHT, "enabled": False},
                },
            },
        ),
        # opencode treats an empty value as unset.
        ("", {"mcp": {"docs": {"enabled": False}, "playwright": {"enabled": False}}}),
    ],
    ids=["object", "empty"],
)
async def test_the_daemon_config_content_is_merged_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, daemon_value: str, expected: dict[str, Any]
) -> None:
    monkeypatch.setenv(_CONTENT, daemon_value)
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    _stub_discovery(monkeypatch, ("docs", "playwright"))

    assert (await _dispatch(_worktree(tmp_path))).ok

    [(_args, env)] = _runs(spawns)
    assert json.loads(env[_CONTENT]) == expected


@pytest.mark.parametrize(
    "daemon_value",
    ["[1, 2]", "not json", '{\n  // the operator\'s note\n  "theme": "dark"\n}'],
    ids=["array", "garbage", "jsonc"],
)
async def test_a_daemon_config_content_that_is_not_an_object_is_left_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    daemon_value: str,
) -> None:
    monkeypatch.setenv(_CONTENT, daemon_value)
    spawns = _capture_spawns(monkeypatch, _fake_cli(tmp_path, monkeypatch))
    _stub_discovery(monkeypatch, ("playwright",))

    assert (await _dispatch(_worktree(tmp_path))).ok

    [(_args, env)] = _runs(spawns)
    assert env[_CONTENT] == daemon_value
    assert {var: env[var] for var in _SKILL_VARS} == dict.fromkeys(_SKILL_VARS, "1")
    assert any(_CONTENT in message for message in _warnings(caplog))


# --- discovery failures --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("debug_config", "discovery_timeout_s", "reason"),
    [
        (
            "sys.stderr.write('\\x1b[91mError: \\x1b[0mconfig is broken\\n')\nsys.exit(3)\n",
            30.0,
            "exit 3: Error: config is broken",  # the stderr tail, without colour codes
        ),
        ("print('{not json')\n", 30.0, "not JSON"),
        ("time.sleep(30)\n", 0.5, "timed out after 0.5s"),
        # The documented V2 shape (servers under mcp.servers), which this provider does not know.
        (
            "print(json.dumps({'mcp': {'servers': {'playwright': {'command': ['npx']}}}}))\n",
            30.0,
            "'servers'",
        ),
    ],
    ids=["exit", "garbage", "timeout", "v2-shape"],
)
async def test_a_failed_discovery_still_dispatches_with_the_skills_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    debug_config: str,
    discovery_timeout_s: float,
    reason: str,
) -> None:
    script = _fake_cli(tmp_path, monkeypatch, debug_config=debug_config)
    spawns = _capture_spawns(monkeypatch, script)
    monkeypatch.setattr(opencode_cli, "_MCP_DISCOVERY_TIMEOUT_S", discovery_timeout_s)
    started = time.monotonic()

    result = await _dispatch(_worktree(tmp_path))

    assert time.monotonic() - started < 10  # a hanging discovery is stopped, not waited for
    assert result.ok and result.text == "done"
    assert [args[0] for args, _env in spawns] == ["debug", "run"]
    [(_args, env)] = _runs(spawns)
    assert {var: env[var] for var in _SKILL_VARS} == dict.fromkeys(_SKILL_VARS, "1")
    assert _CONTENT not in env
    [warning] = _warnings(caplog)
    assert "opencode debug config" in warning and reason in warning


async def test_a_missing_cli_fails_the_dispatch_as_before(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    result = await _dispatch(_worktree(tmp_path))  # the autouse fixture's missing binary

    assert result.ok is False
    assert (result.error or "").startswith("CLI not found")
    [warning] = _warnings(caplog)
    assert "cannot start" in warning


def test_mcp_server_names_reads_opencode_1x_entries() -> None:
    config = {
        "model": "deepseek/deepseek-flash",
        "mcp": {
            "zeta": {"type": "remote", "url": "https://mcp.example.test/mcp"},
            "alpha": {"type": "local", "command": ["npx", "alpha"], "enabled": True},
            "mid": {"enabled": False},  # only turns off a server defined elsewhere
        },
    }

    assert _mcp_server_names(json.dumps(config)) == ("alpha", "mid", "zeta")
    assert _mcp_server_names('{"model": "x"}') == ()
    assert _mcp_server_names('{"mcp": {}}') == ()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "garbage",
        "[1, 2]",
        '{"mcp": []}',
        '{"mcp": {"servers": {"playwright": {"type": "local", "command": ["npx"]}}}}',
        '{"mcp": {"x": {"enabled": "nope"}}}',
    ],
)
def test_mcp_server_names_rejects_unknown_shapes(output: str) -> None:
    with pytest.raises(_McpDiscoveryError):
        _mcp_server_names(output)


# --- the discovery cache -------------------------------------------------------------------------


async def test_discovery_runs_once_per_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(tmp_path, monkeypatch)
    calls = _stub_discovery(monkeypatch, ("playwright",))
    provider = OpenCodeCliProvider()
    first, second = _worktree(tmp_path, "a"), _worktree(tmp_path, "b")

    for cwd in (first, first, second):
        assert (await _dispatch(cwd, provider=provider)).ok

    assert [cwd for cwd, _env in calls] == [first, second]


async def test_a_failed_discovery_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_cli(tmp_path, monkeypatch)
    calls = _stub_discovery(monkeypatch, _McpDiscoveryError("exit 1"))
    provider = OpenCodeCliProvider()
    cwd = _worktree(tmp_path)

    for _ in range(2):
        assert (await _dispatch(cwd, provider=provider)).ok

    assert len(calls) == 2
    assert len(_warnings(caplog)) == 2


async def test_the_discovery_cache_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(tmp_path, monkeypatch)
    calls = _stub_discovery(monkeypatch, ("playwright",))
    monkeypatch.setattr(opencode_cli, "_MCP_CACHE_MAX", 2)
    provider = OpenCodeCliProvider()
    a, b, c = (_worktree(tmp_path, name) for name in "abc")

    for cwd in (a, b, c, a):  # c pushes a out
        assert (await _dispatch(cwd, provider=provider)).ok

    assert [cwd for cwd, _env in calls] == [a, b, c, a]


async def test_a_transient_retry_reuses_the_environment_without_rediscovering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = tmp_path / "attempts.txt"
    error = {"type": "error", "error": {"data": {"message": "Cannot connect to API"}}}
    script = _fake_cli(
        tmp_path,
        monkeypatch,
        run=(
            f"p = pathlib.Path({str(attempts)!r})\n"
            "n = int(p.read_text()) if p.exists() else 0\n"
            "p.write_text(str(n + 1))\n"
            "if n == 0:\n"
            f"    sys.stdout.write(json.dumps({error!r}) + '\\n')\n"
            "    sys.exit(1)\n"
            f"sys.stdout.write(json.dumps({_TEXT_EVENT!r}) + '\\n')\n"
        ),
    )
    spawns = _capture_spawns(monkeypatch, script)
    calls = _stub_discovery(monkeypatch, ("playwright",))
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)

    result = await _dispatch(_worktree(tmp_path))

    assert result.ok and result.text == "done"
    [(_first_args, first_env), (_second_args, second_env)] = _runs(spawns)
    assert first_env == second_env
    assert json.loads(first_env[_CONTENT]) == {"mcp": {"playwright": {"enabled": False}}}
    assert len(calls) == 1


# --- end to end, through a fake `opencode` on PATH -----------------------------------------------

_E2E_CONFIG = {
    "model": "deepseek/deepseek-flash",
    "mcp": {
        "playwright": {"type": "local", "command": ["npx", "-y", "@playwright/mcp"]},
        "docs": {"type": "remote", "url": "https://mcp.example.test/mcp", "enabled": True},
    },
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang script")
async def test_an_isolated_dispatch_end_to_end_through_a_fake_opencode_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "started.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "opencode"
    # The interpreter is found next to sys.executable, the only other PATH entry.
    fake.write_text(
        f"#!/usr/bin/env {Path(sys.executable).name}\n"
        "import json, os, sys\n"
        "def record():\n"
        f"    with open({str(record)!r}, 'a', encoding='utf-8') as f:\n"
        f"        env = {{key: os.environ.get(key) for key in {list(_ISOLATION_VARS)!r}}}\n"
        "        f.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': env}))\n"
        "        f.write('\\n')\n"
        "if sys.argv[1:] == ['debug', 'config']:\n"
        "    record()\n"
        f"    print(json.dumps({_E2E_CONFIG!r}, indent=2))\n"
        "    sys.exit(0)\n"
        "sys.stdin.buffer.read()\n"
        "record()\n"
        f"sys.stdout.write(json.dumps({_TEXT_EVENT!r}) + '\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)]))
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", "opencode")
    provider = OpenCodeCliProvider()
    cwd = _worktree(tmp_path)

    results = [
        await _dispatch(cwd, provider=provider),
        await _dispatch(cwd, provider=provider),  # same worktree: no second discovery
        await _dispatch(cwd, provider=provider, isolated=False),
    ]

    assert [result.text for result in results] == ["done"] * 3
    started = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    discoveries = [s for s in started if s["argv"] == ["debug", "config"]]
    runs = [s for s in started if s["argv"][:1] == ["run"]]
    assert len(discoveries) == 1 and len(runs) == 3
    [discovery] = discoveries
    assert Path(discovery["cwd"]).resolve() == cwd.resolve()
    assert discovery["env"] == dict.fromkeys(_ISOLATION_VARS)  # none of the overrides
    disabled = {"mcp": {"docs": {"enabled": False}, "playwright": {"enabled": False}}}
    for isolated_run in runs[:2]:
        env = isolated_run["env"]
        assert {var: env[var] for var in _SKILL_VARS} == dict.fromkeys(_SKILL_VARS, "1")
        assert json.loads(env[_CONTENT]) == disabled
    assert runs[2]["env"] == dict.fromkeys(_ISOLATION_VARS)
    assert runs[0]["argv"] == runs[1]["argv"] == runs[2]["argv"]
    assert Path(runs[0]["cwd"]).resolve() == cwd.resolve()
