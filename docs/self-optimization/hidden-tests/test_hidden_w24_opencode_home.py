"""Hidden acceptance tests for W24, written by the pilot from the spec alone.

An isolated opencode dispatch runs in a bubblewrap sandbox with Conclave-owned opencode folders,
one set per provider. It sees only its own provider's config block and credential, nothing else
of the owner's opencode setup, and writes nothing into the owner's opencode folders. The commands
it runs see the same environment and the owner's other tool setup.

The fake ``opencode`` resolves its folders the way opencode 1.19 does (``XDG_*``, else ``HOME``),
gives ``OPENCODE_AUTH_CONTENT`` precedence over ``auth.json``, writes a log line and a session like
opencode, and reports what it can see. Run against a W24 snapshot with
``PYTHONPATH=<snap>/src:<snap>/tests/integration``. The sandbox tests need a working bubblewrap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import AgentResult, opencode_cli
from conclave.providers.opencode_cli import OpenCodeCliProvider
from conclave.providers.profiles import ResolvedProfile

_DEEPSEEK_KEY = "sk-w24-deepseek-sentinel"
_ANTHROPIC_KEY = "sk-w24-anthropic-sentinel"
_OAUTH_REFRESH = "w24-oauth-refresh-sentinel"
_OWNER_MODEL = "local-llm/coder"
_OWNER_PERMISSION = "w24_owner_permission_rule"
_OWNER_RULES = "W24 OWNER GLOBAL RULES SENTINEL"
_OWNER_CLAUDE_MD = "W24 OWNER CLAUDE MD SENTINEL"
_STRAY_PROVIDER = "w24-stray-provider"
_KINDS = {
    "config": "XDG_CONFIG_HOME",
    "data": "XDG_DATA_HOME",
    "state": "XDG_STATE_HOME",
    "cache": "XDG_CACHE_HOME",
}


def _bwrap_works() -> bool:
    binary = shutil.which("bwrap")
    if binary is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "--dev-bind", "/", "/", "--", "true"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


needs_sandbox = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap is not usable here")


def _mode(value: str) -> Any:
    """``value`` as the spec's ``WorkerSandbox`` member (a plain string if it is missing)."""
    try:
        from conclave.config import WorkerSandbox  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - the snapshot must have it
        return value
    return WorkerSandbox(value)


_FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, shlex, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
KINDS = {"config": ("XDG_CONFIG_HOME", ".config"), "data": ("XDG_DATA_HOME", ".local/share"),
         "state": ("XDG_STATE_HOME", ".local/state"), "cache": ("XDG_CACHE_HOME", ".cache")}

def home(kind):
    var, default = KINDS[kind]
    base = os.environ.get(var) or os.path.join(os.environ.get("HOME", "/nonexistent"), default)
    return pathlib.Path(base) / "opencode"

def merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base

def load(path):
    return json.loads(path.read_text()) if path.is_file() else {}

def resolved():
    config = {}
    for name in ("config.json", "opencode.json", "opencode.jsonc"):
        merge(config, load(home("config") / name))
    if os.environ.get("OPENCODE_CONFIG"):
        merge(config, load(pathlib.Path(os.environ["OPENCODE_CONFIG"])))
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        for name in ("opencode.json", "opencode.jsonc"):
            merge(config, load(pathlib.Path(os.environ["OPENCODE_CONFIG_DIR"]) / name))
    if os.environ.get("OPENCODE_CONFIG_CONTENT"):
        merge(config, json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]))
    return config

def credentials():
    content = os.environ.get("OPENCODE_AUTH_CONTENT")
    if content:
        try:
            return json.loads(content)
        except ValueError:
            pass
    return load(home("data") / "auth.json")

def instructions():
    found = {}
    for path in (home("config") / "AGENTS.md",
                 pathlib.Path(os.environ.get("HOME", "/nonexistent")) / ".claude" / "CLAUDE.md"):
        if path.is_file():
            found[str(path)] = path.read_text()
    return found

def files():
    seen = {}
    for kind in KINDS:
        for folder, _dirs, names in os.walk(home(kind)):
            for name in names:
                path = pathlib.Path(folder) / name
                try:
                    seen[str(path)] = path.read_text()
                except (UnicodeDecodeError, OSError):
                    seen[str(path)] = ""
    return seen

PROBE = (
    "import json, os, pathlib\n"
    "kinds = {'config': ('XDG_CONFIG_HOME', '.config'), 'data': ('XDG_DATA_HOME', '.local/share'),"
    " 'state': ('XDG_STATE_HOME', '.local/state'), 'cache': ('XDG_CACHE_HOME', '.cache')}\n"
    "out = {'xdg': {}, 'markers': {}}\n"
    "for kind, (var, default) in kinds.items():\n"
    "    out['xdg'][var] = os.environ.get(var)\n"
    "    base = os.environ.get(var) or os.path.join(os.environ['HOME'], default)\n"
    "    marker = pathlib.Path(base) / 'w24tool' / 'marker'\n"
    "    out['markers'][kind] = marker.read_text() if marker.is_file() else None\n"
    "print(json.dumps(out))\n"
)

def command_view(config):
    # Like opencode's bash tool: the configured shell, else $SHELL, with this process's env.
    shell = config.get("shell") or os.environ.get("SHELL") or "/bin/sh"
    line = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(PROBE)}"
    done = subprocess.run([shell, "-c", line], env={**os.environ, "TERM": "dumb"},
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        return {"error": done.stderr}
    return json.loads(done.stdout.strip().splitlines()[-1])

log = home("data") / "log"
log.mkdir(parents=True, exist_ok=True)
with open(log / "opencode.log", "a", encoding="utf-8") as out:
    out.write(f"{time.time()} {' '.join(sys.argv[1:])}\n")

args = sys.argv[1:]
if args == ["debug", "config"]:
    print(json.dumps(resolved()))
    sys.exit(0)
if args == ["debug", "paths"]:
    for kind in ("data", "config", "state", "cache"):
        print(f"{kind:<10} {home(kind)}")
    sys.exit(0)
if args[:1] == ["run"]:
    sys.stdin.buffer.read()
    config = resolved()
    auth_path = home("data") / "auth.json"
    report = {
        "args": args,
        "env": dict(os.environ),
        "config": config,
        "credentials": credentials(),
        "auth_mode": oct(auth_path.stat().st_mode & 0o777) if auth_path.is_file() else None,
        "files": files(),
        "instructions": instructions(),
        "command": command_view(config),
    }
    sessions = home("data") / "storage" / "session"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"ses_w24_{time.time_ns()}.json").write_text("{}")
    with open(HERE / "reports.jsonl", "a", encoding="utf-8") as out:
        out.write(json.dumps(report) + "\n")
    child_pid_file = os.environ.get("W24_CHILD_PID_FILE")
    if child_pid_file:
        child = subprocess.Popen(["sleep", "300"])
        pathlib.Path(child_pid_file).write_text(str(child.pid))
        time.sleep(300)
    time.sleep(0.5)  # long enough for concurrent dispatches to overlap
    sys.stdout.write(json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}) + "\n")
'''


def _owner_credentials() -> dict[str, Any]:
    return {
        "deepseek": {"type": "api", "key": _DEEPSEEK_KEY},
        "anthropic": {"type": "api", "key": _ANTHROPIC_KEY},
        "copilot": {"type": "oauth", "refresh": _OAUTH_REFRESH, "access": "a", "expires": 1},
    }


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The owner's opencode setup in temporary homes, Conclave's home inside the owner's data
    folder (as with the default ``~/.local/share/conclave``), and the fake CLI installed."""
    for var in list(os.environ):
        if var.startswith("OPENCODE_"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CONCLAVE_BWRAP_BIN", raising=False)
    monkeypatch.delenv("W24_CHILD_PID_FILE", raising=False)
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("SHELL", "/bin/sh")
    homes: dict[str, Path] = {}
    for kind, var in _KINDS.items():
        homes[kind] = tmp_path / "owner-xdg" / kind
        homes[kind].mkdir(parents=True)
        monkeypatch.setenv(var, str(homes[kind]))
        (homes[kind] / "w24tool").mkdir()
        (homes[kind] / "w24tool" / "marker").write_text(f"{kind}-marker")
    conclave_home = homes["data"] / "conclave"
    monkeypatch.setenv("CONCLAVE_HOME", str(conclave_home))

    config_dir = homes["config"] / "opencode"
    config_dir.mkdir()
    (config_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": _OWNER_MODEL,
                "permission": {_OWNER_PERMISSION: "allow"},
                "mcp": {"owner-browser": {"type": "local", "command": ["browser-mcp"]}},
                "provider": {
                    "deepseek": {"options": {"timeout": 600000}},
                    "anthropic": {"options": {"timeout": 1}},
                    "local-llm": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://127.0.0.1:9999/v1"},
                        "models": {"coder": {"name": "Coder"}},
                    },
                },
            }
        )
    )
    (config_dir / "AGENTS.md").write_text(_OWNER_RULES)
    (owner_home / ".claude").mkdir()
    (owner_home / ".claude" / "CLAUDE.md").write_text(_OWNER_CLAUDE_MD)

    data_dir = homes["data"] / "opencode"
    (data_dir / "storage" / "session").mkdir(parents=True)
    (data_dir / "storage" / "session" / "ses_owner.json").write_text('{"owner": true}')
    (data_dir / "opencode.db").write_text("owner-db")
    (data_dir / "log").mkdir()
    (data_dir / "log" / "opencode.log").write_text("owner log\n")
    auth = data_dir / "auth.json"
    auth.write_text(json.dumps(_owner_credentials()))
    auth.chmod(0o600)
    (homes["state"] / "opencode").mkdir()
    (homes["state"] / "opencode" / "model.json").write_text('{"recent": ["local-llm/coder"]}')

    script = tmp_path / "bin" / "opencode"
    script.parent.mkdir()
    script.write_text(_FAKE)
    script.chmod(0o755)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(script))
    return {"script": script, "home": owner_home, "conclave_home": conclave_home, **homes}


async def _dispatch(
    tmp_path: Path,
    model: str | None,
    *,
    isolated: bool = True,
    sandbox: str = "auto",
    extra_env: dict[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AgentResult:
    cwd = tmp_path / "worktree"
    cwd.mkdir(exist_ok=True)
    profile = ResolvedProfile(
        name="opencode",
        arg_mode=ArgMode.inherit,
        isolated=isolated,
        model=model,
        extra_env=extra_env or {},
        sandbox=_mode(sandbox),  # type: ignore[call-arg]
    )
    return await OpenCodeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=120, cwd=cwd, cancel_event=cancel_event
    )


def _reports(owner: dict[str, Path]) -> list[dict[str, Any]]:
    path = owner["script"].parent / "reports.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _visible(report: dict[str, Any]) -> str:
    """Everything the worker can read: environment, effective config, credentials, the files in
    its opencode folders and the instruction files opencode would load."""
    return "\n".join(
        [
            json.dumps(report["env"]),
            json.dumps(report["config"]),
            json.dumps(report["credentials"]),
            *report["files"].values(),
            *report["instructions"].values(),
        ]
    )


def _owner_snapshot(owner: dict[str, Path]) -> dict[str, str]:
    """sha256 of every file in the owner's homes, Conclave's own home excluded."""
    conclave = owner["conclave_home"].resolve()
    digest: dict[str, str] = {}
    for root in (owner["home"], *(owner[kind] for kind in _KINDS)):
        for folder, dirs, names in os.walk(root):
            here = Path(folder).resolve()
            if here == conclave or conclave in here.parents:
                dirs[:] = []
                continue
            for name in names:
                path = Path(folder) / name
                if path.is_symlink():
                    digest[str(path)] = "symlink:" + os.readlink(path)
                else:
                    digest[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _session_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder, _dirs, names in os.walk(root):
        found += [Path(folder) / n for n in names if n.startswith("ses_w24_")]
    return found


def _model_of(report: dict[str, Any]) -> str:
    args: list[str] = report["args"]
    return args[args.index("--model") + 1]


# --- the owner's folders -----------------------------------------------------------------------


@needs_sandbox
async def test_a_sandboxed_dispatch_leaves_the_owners_opencode_files_untouched(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    before = _owner_snapshot(owner)
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat")
    assert result.ok, result.error
    assert _owner_snapshot(owner) == before  # no session, log, lock or credential written
    assert len(_session_files(owner["conclave_home"])) == 1  # the session is Conclave's


@needs_sandbox
async def test_one_provider_keeps_its_sessions_and_never_sees_the_owners(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    assert (await _dispatch(tmp_path, "deepseek/deepseek-chat")).ok
    assert (await _dispatch(tmp_path, "deepseek/deepseek-chat")).ok
    _first, second = _reports(owner)
    seen = [Path(f).name for f in second["files"]]
    assert sum(name.startswith("ses_w24_") for name in seen) == 1  # the first dispatch's
    assert "ses_owner.json" not in seen
    assert "owner log" not in _visible(second)


# --- what the worker sees -------------------------------------------------------------------


@needs_sandbox
async def test_the_worker_sees_only_its_providers_block_and_credential(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat")
    assert result.ok, result.error
    [report] = _reports(owner)
    visible = _visible(report)

    assert _DEEPSEEK_KEY in visible
    assert _ANTHROPIC_KEY not in visible
    assert _OAUTH_REFRESH not in visible
    assert set(report["credentials"]) == {"deepseek"}
    if report["auth_mode"] is not None:
        assert report["auth_mode"] == "0o600"

    config = report["config"]
    assert set(config.get("provider", {})) == {"deepseek"}
    assert config["provider"]["deepseek"]["options"]["timeout"] == 600000
    assert config.get("model") != _OWNER_MODEL
    assert _OWNER_PERMISSION not in visible
    for name, server in config.get("mcp", {}).items():
        assert server.get("enabled") is False, f"MCP server {name} reaches the worker"
    assert _OWNER_RULES not in visible  # <config>/opencode/AGENTS.md
    assert _OWNER_CLAUDE_MD not in visible  # ~/.claude/CLAUDE.md


@needs_sandbox
async def test_a_local_provider_gets_its_block_and_no_credential(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    result = await _dispatch(tmp_path, "local-llm/coder")
    assert result.ok, result.error
    [report] = _reports(owner)
    assert set(report["config"]["provider"]) == {"local-llm"}
    assert report["config"]["provider"]["local-llm"]["options"]["baseURL"] == (
        "http://127.0.0.1:9999/v1"
    )
    assert report["credentials"] == {}
    assert _DEEPSEEK_KEY not in _visible(report) and _ANTHROPIC_KEY not in _visible(report)


@needs_sandbox
async def test_concurrent_providers_each_see_only_their_own_credential(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    results = await asyncio.gather(
        _dispatch(tmp_path, "deepseek/deepseek-chat"),
        _dispatch(tmp_path, "anthropic/claude-x"),
        _dispatch(tmp_path, "deepseek/deepseek-chat"),
    )
    assert all(r.ok for r in results), [r.error for r in results]
    reports = _reports(owner)
    assert sorted(_model_of(r) for r in reports) == [
        "anthropic/claude-x",
        "deepseek/deepseek-chat",
        "deepseek/deepseek-chat",
    ]
    for report in reports:
        visible = _visible(report)
        if _model_of(report).startswith("deepseek/"):
            assert _DEEPSEEK_KEY in visible and _ANTHROPIC_KEY not in visible
        else:
            assert _ANTHROPIC_KEY in visible and _DEEPSEEK_KEY not in visible


@needs_sandbox
async def test_an_oauth_credential_is_refused_before_anything_starts(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    before = _owner_snapshot(owner)
    result = await _dispatch(tmp_path, "copilot/gpt-5")
    assert not result.ok
    assert result.error is not None and "copilot" in result.error
    assert _reports(owner) == []
    assert _owner_snapshot(owner) == before


@needs_sandbox
async def test_without_a_model_the_owners_default_model_decides(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    result = await _dispatch(tmp_path, None)
    assert result.ok, result.error
    [report] = _reports(owner)
    assert _model_of(report) == _OWNER_MODEL
    assert set(report["config"]["provider"]) == {"local-llm"}


# --- the daemon's environment -----------------------------------------------------------------


@needs_sandbox
async def test_inherited_opencode_variables_do_not_reach_a_sandboxed_worker(
    owner: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "opencode.json").write_text(json.dumps({"provider": {_STRAY_PROVIDER: {}}}))
    monkeypatch.setenv("OPENCODE_CONFIG", str(stray / "opencode.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(stray))
    monkeypatch.setenv("OPENCODE_AUTH_CONTENT", json.dumps(_owner_credentials()))
    monkeypatch.setenv("OPENCODE_DB", str(owner["data"] / "opencode" / "opencode.db"))

    result = await _dispatch(tmp_path, "deepseek/deepseek-chat")
    assert result.ok, result.error
    [report] = _reports(owner)
    for var in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_CONTENT", "OPENCODE_DB"):
        assert var not in report["env"], var
    assert _STRAY_PROVIDER not in json.dumps(report["config"])
    assert _DEEPSEEK_KEY in _visible(report)  # the owner's credential source was the variable
    assert _ANTHROPIC_KEY not in _visible(report)


@needs_sandbox
async def test_extra_env_may_set_a_dropped_variable(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    mine = str(tmp_path / "my-sessions.db")
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat", extra_env={"OPENCODE_DB": mine})
    assert result.ok, result.error
    [report] = _reports(owner)
    assert report["env"]["OPENCODE_DB"] == mine


@needs_sandbox
async def test_the_workers_commands_see_the_owners_tool_setup(
    owner: dict[str, Path], tmp_path: Path
) -> None:
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat")
    assert result.ok, result.error
    [report] = _reports(owner)
    command = report["command"]
    assert "error" not in command, command
    assert command["xdg"] == {var: str(owner[kind]) for kind, var in _KINDS.items()}
    assert command["markers"] == {kind: f"{kind}-marker" for kind in _KINDS}
    for kind, var in _KINDS.items():
        assert report["env"][var] == str(owner[kind])  # opencode's own environment is unchanged


# --- process lifetime -------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


@needs_sandbox
async def test_cancel_leaves_no_process_of_a_sandboxed_dispatch_behind(
    owner: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("W24_CHILD_PID_FILE", str(pid_file))
    cancel = asyncio.Event()
    running = asyncio.create_task(
        _dispatch(tmp_path, "deepseek/deepseek-chat", cancel_event=cancel)
    )
    deadline = time.monotonic() + 60
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the fake never started its child"
        assert not running.done(), running.result()
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text())
    cancel.set()
    result = await asyncio.wait_for(running, timeout=60)
    assert not result.ok
    deadline = time.monotonic() + 15
    while _alive(child):
        assert time.monotonic() < deadline, f"process {child} survived the cancel"
        await asyncio.sleep(0.05)


# --- without a sandbox ------------------------------------------------------------------------


async def test_required_without_bubblewrap_fails_before_anything_starts(
    owner: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", str(tmp_path / "no-bwrap"))
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat", sandbox="required")
    assert not result.ok
    error = (result.error or "").lower()
    assert ("bubblewrap" in error or "bwrap" in error) and "worker_sandbox" in error
    assert _reports(owner) == []


async def test_auto_without_bubblewrap_runs_as_before(
    owner: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", str(tmp_path / "no-bwrap"))
    result = await _dispatch(tmp_path, "deepseek/deepseek-chat", sandbox="auto")
    assert result.ok, result.error
    [report] = _reports(owner)
    assert report["config"].get("model") == _OWNER_MODEL  # the owner's config, as in W10
    assert report["config"]["mcp"]["owner-browser"].get("enabled") is False  # W10 still applies


async def test_off_and_not_isolated_run_as_before(owner: dict[str, Path], tmp_path: Path) -> None:
    assert (await _dispatch(tmp_path, "deepseek/deepseek-chat", sandbox="off")).ok
    assert (await _dispatch(tmp_path, "deepseek/deepseek-chat", isolated=False)).ok
    off, unisolated = _reports(owner)
    assert off["config"].get("model") == _OWNER_MODEL
    assert off["config"]["mcp"]["owner-browser"].get("enabled") is False
    assert unisolated["config"].get("model") == _OWNER_MODEL
    assert unisolated["config"]["mcp"]["owner-browser"].get("enabled") is not False
    assert _ANTHROPIC_KEY in _visible(unisolated)  # unchanged: the owner's whole setup
