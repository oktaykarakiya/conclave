"""The worker sandbox of opencode dispatches (``execution.worker_sandbox``).

A sandboxed dispatch runs opencode under bubblewrap with Conclave's own opencode folders for its
model provider mounted over the operator's: only that provider's config block and credential,
none of the operator's sessions, config or instructions, and every other path as it is.

Every test runs a fake ``opencode`` that resolves its folders as opencode does (XDG, else
``HOME``), reads ``OPENCODE_AUTH_CONTENT`` before ``auth.json``, writes a ``.gitignore`` into its
config folder, a missing ``$schema`` back into each config file, a session file and a log line
into its data folder, runs one shell command as opencode's bash tool does, and reports what it
saw. The operator's ``HOME`` and ``XDG_*`` folders are temporary; ``CONCLAVE_HOME`` is inside the
temporary data folder. Tests that need a working bubblewrap skip without one; the others point
``CONCLAVE_BWRAP_BIN`` at a missing file or a stand-in. The real CLI never starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode, WorkerSandbox
from conclave.providers import AgentResult, opencode_cli
from conclave.providers.opencode_cli import OpenCodeCliProvider, _operator_config_of
from conclave.providers.profiles import ResolvedProfile
from conclave.providers.sandbox import (
    OpencodeFolders,
    SandboxError,
    bwrap_command,
    discovery_mounts,
    dispatch_mounts,
    hidden_file_mounts,
    opencode_folders,
    opencode_homes,
    operator_credentials,
    prepare_provider_folders,
    provider_credential,
    provider_folder_name,
    provider_folders,
    sandbox_status,
    setup_failure,
)
from waits import wait_until

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the worker sandbox is Linux-only")

_SANDBOX = sandbox_status() if sys.platform == "linux" else None
needs_bwrap = pytest.mark.skipif(
    _SANDBOX is None or not _SANDBOX.available,
    reason=f"the worker sandbox is unavailable here: {_SANDBOX.reason if _SANDBOX else ''}",
)

_LOGGER = "conclave.providers.opencode_cli"
# Variables of the daemon's environment that would point a worker at the operator's own setup.
_OPERATOR_SETUP_VARS = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_AUTH_CONTENT",
    "OPENCODE_DB",
)
_ISOLATION_VARS = (
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_CONFIG_CONTENT",
)

# The operator's global opencode config: several providers, a default model, a permission rule,
# an MCP server and an instruction file. It has no $schema, which opencode writes into each
# config file it reads that lacks one: the operator's must stay as it is all the same.
_OPERATOR_CONFIG: dict[str, Any] = {
    "model": "llamacpp/qwen3-coder",
    "permission": {"bash": "ask"},
    "mcp": {"playwright": {"type": "local", "command": ["npx", "-y", "@playwright/mcp"]}},
    "instructions": ["operator-rules.md"],
    "provider": {
        "llamacpp": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "llama.cpp",
            "options": {"baseURL": "http://127.0.0.1:8080/v1"},
            "models": {"qwen3-coder": {"name": "Qwen3 Coder"}},
        },
        # opencode debug config prints the key redacted ("***"): the worker gets no such value.
        "deepseek": {"options": {"timeout": 600000, "apiKey": "sk-from-the-config"}},
        "openrouter": {"options": {"headers": {"HTTP-Referer": "https://conclave.invalid"}}},
    },
}
_DEEPSEEK_BLOCK = {"options": {"timeout": 600000}}
_LLAMACPP_BLOCK = _OPERATOR_CONFIG["provider"]["llamacpp"]
_OPERATOR_CREDENTIALS: dict[str, Any] = {
    "deepseek": {"type": "api", "key": "sk-deepseek"},
    "openrouter": {"type": "api", "key": "sk-openrouter"},
    "anthropic": {"type": "oauth", "refresh": "r-token", "access": "a-token", "expires": 0},
}
# What `ls -A ~/.claude` shows a worker's command: everything but CLAUDE.md.
_CLAUDE_ENTRIES = ["projects", "settings.json", "skills"]

# One shell command, run as opencode's bash tool runs it (the process's environment).
_COMMAND = "\n".join(
    [
        *(f'echo "{var}=${var}"' for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                              "XDG_STATE_HOME", "XDG_CACHE_HOME", "HOME")),
        'echo "git=$(cat "$XDG_CONFIG_HOME/git/config")"',
        "echo \"claude=$(ls -A \"$HOME/.claude\" | tr '\\n' ' ')\"",
    ]
)

_FAKE_OPENCODE = r'''
import json, os, pathlib, re, subprocess, sys, time, uuid
from urllib.parse import parse_qsl, urlsplit

argv = sys.argv[1:]
home = os.environ.get("HOME") or os.path.expanduser("~")


def folder(variable, *default):
    return pathlib.Path(os.environ.get(variable) or os.path.join(home, *default)) / "opencode"


config_dir = folder("XDG_CONFIG_HOME", ".config")
data_dir = folder("XDG_DATA_HOME", ".local", "share")
state_dir = folder("XDG_STATE_HOME", ".local", "state")
for path in (config_dir, data_dir, state_dir):
    path.mkdir(parents=True, exist_ok=True)  # as opencode does at startup
# As opencode does for each config folder: a .gitignore where there is none. It ignores a
# refused permission, and stops on any other failure (a read-only folder, say).
gitignore = config_dir / ".gitignore"
if not gitignore.exists():
    try:
        gitignore.write_text("node_modules\n")
    except PermissionError:
        pass
    except OSError as exc:
        sys.exit(f"Error: FileSystem.writeFile ({gitignore}): {exc}")


def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return None


def read_config(path):
    """A config file, whose missing $schema opencode writes back (a failed write is ignored)."""
    value = read_json(path)
    if isinstance(value, dict) and "$schema" not in value:
        try:
            text = json.dumps({"$schema": "https://opencode.ai/config.json", **value}, indent=2)
            pathlib.Path(path).write_text(text)
        except OSError:
            pass
    return value


def merged(base, patch):
    out = dict(base)
    for key, value in patch.items():
        both = isinstance(out.get(key), dict) and isinstance(value, dict)
        out[key] = merged(out[key], value) if both else value
    return out


def resolved_config(cwd):
    sources = [config_dir / "opencode.json"]
    if os.environ.get("OPENCODE_CONFIG"):
        sources.append(os.environ["OPENCODE_CONFIG"])
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        sources.append(pathlib.Path(os.environ["OPENCODE_CONFIG_DIR"]) / "opencode.json")
    sources.append(pathlib.Path(cwd) / "opencode.json")
    config = {}
    for source in sources:
        value = read_config(source)
        if isinstance(value, dict):
            config = merged(config, value)
    if os.environ.get("OPENCODE_CONFIG_CONTENT"):
        config = merged(config, json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]))
    return config


def credentials():
    if os.environ.get("OPENCODE_AUTH_CONTENT"):
        try:
            return json.loads(os.environ["OPENCODE_AUTH_CONTENT"])
        except ValueError:
            pass
    return read_json(data_dir / "auth.json") or {}


SECRET = re.compile(
    r"(?i)(?:api.?key|secret|password|token$|authorization$|cookie$|credential|private.?key)"
)


def secret_url(url):
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return True
    query = parse_qsl(parts.query)
    return bool(parts.username or parts.password or any(SECRET.search(k) for k, _ in query))


def redacted(value, everything=False):  # as opencode debug config prints a config
    if isinstance(value, list):
        return [redacted(item, everything) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if isinstance(item, str) and (everything or SECRET.search(key)):
            out[key] = "***"
        elif isinstance(item, str) and re.match(r"(?i)https?://", item) and secret_url(item):
            out[key] = "***"
        else:
            out[key] = redacted(item, everything or key.lower() == "headers")
    return out


def shown(config):  # what a test compares: the config without the $schema opencode adds
    return {key: value for key, value in config.items() if key != "$schema"}


def files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def report(entry):
    with open(__REPORT__, "a", encoding="utf-8") as out:
        out.write(json.dumps(entry) + "\n")


(data_dir / "log").mkdir(exist_ok=True)
with open(data_dir / "log" / "opencode.log", "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")

if argv[:2] == ["debug", "config"]:
    report({"kind": "debug", "cwd": os.getcwd(), "env": dict(os.environ)})
    print(json.dumps(redacted(resolved_config(os.getcwd())), indent=2))
    sys.exit(0)

prompt = sys.stdin.read()
option = lambda name: argv[argv.index(name) + 1] if name in argv else None
cwd = option("--dir") or os.getcwd()
sessions = data_dir / "sessions"
sessions.mkdir(exist_ok=True)
session = option("--session") or "ses_" + uuid.uuid4().hex[:12]
known = (sessions / (session + ".json")).exists()
(sessions / (session + ".json")).write_text(json.dumps({"id": session, "model": option("--model")}))
shell = subprocess.run(
    ["sh", "-c", __COMMAND__],
    env={**os.environ, "TERM": "dumb"}, capture_output=True, text=True, cwd=cwd,
)
report({
    "kind": "run",
    "argv": argv,
    "env": dict(os.environ),
    "config": shown(resolved_config(cwd)),
    "credentials": credentials(),
    "files": {"config": files(config_dir), "data": files(data_dir), "state": files(state_dir)},
    "session": session,
    "session_known": known,
    "claude_md": os.path.lexists(os.path.join(home, ".claude", "CLAUDE.md")),
    "command": dict(line.split("=", 1) for line in shell.stdout.splitlines() if "=" in line),
})
if "SPAWN-CHILD" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    pathlib.Path(__CHILD__).write_text(f"{os.getpid()} {child.pid}")
    time.sleep(300)
event = {"type": "text", "sessionID": session, "part": {"type": "text", "text": "done"}}
sys.stdout.write(json.dumps(event) + "\n")
'''


def _snapshot(*roots: Path, skip: Path) -> dict[str, tuple[str, object]]:
    """Each of *roots* and everything under the folder it is or leads to, but the folder *skip*:
    a symlink as its target, a folder as its mode, a file as its bytes."""
    found: dict[str, tuple[str, object]] = {}
    skipped = skip.resolve()

    def record(path: Path) -> None:
        if path.is_symlink():
            found[str(path)] = ("link", os.readlink(path))
        elif path.is_dir():
            found[str(path)] = ("folder", stat.S_IMODE(path.stat().st_mode))
        elif path.exists():
            found[str(path)] = ("file", path.read_bytes())

    for root in roots:
        record(root)
        real = root.resolve()
        if not real.is_dir():
            continue
        for folder, folders, files in os.walk(real):
            folders[:] = [name for name in folders if Path(folder, name) != skipped]
            for name in [*folders, *files]:
                record(Path(folder, name))
    return found


@dataclass(frozen=True)
class _Host:
    """The temporary operator: home and XDG folders, the fake CLI and what it reports."""

    root: Path
    home: Path
    config: Path  # $XDG_CONFIG_HOME
    data: Path  # $XDG_DATA_HOME
    state: Path  # $XDG_STATE_HOME
    cache: Path  # $XDG_CACHE_HOME
    conclave_home: Path
    report: Path
    child: Path  # where the fake writes "<its pid> <its child's pid>"
    worktree: Path

    @property
    def operator(self) -> OpencodeFolders:
        return OpencodeFolders(
            config=self.config / "opencode",
            data=self.data / "opencode",
            state=self.state / "opencode",
        )

    def own(self, provider: str) -> OpencodeFolders:
        return provider_folders(self.conclave_home / "opencode-homes", provider)

    def reports(self, kind: str) -> list[dict[str, Any]]:
        if not self.report.exists():
            return []
        entries = [json.loads(line) for line in self.report.read_text().splitlines()]
        return [entry for entry in entries if entry["kind"] == kind]

    def operator_snapshot(self) -> dict[str, tuple[str, object]]:
        """The operator's opencode folders, ~/.claude and the dotfiles they may link into (where
        a linked data folder takes $CONCLAVE_HOME, which is not the operator's)."""
        return _snapshot(
            *vars(self.operator).values(),
            self.home / ".claude",
            self.root / "dotfiles",
            skip=self.conclave_home,
        )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    host = _Host(
        root=tmp_path,
        home=tmp_path / "home",
        config=tmp_path / "xdg-config",
        data=tmp_path / "xdg-data",
        state=tmp_path / "xdg-state",
        cache=tmp_path / "xdg-cache",
        conclave_home=tmp_path / "xdg-data" / "conclave",
        report=tmp_path / "report.jsonl",
        child=tmp_path / "child.txt",
        worktree=tmp_path / "worktree",
    )
    for var in (*_OPERATOR_SETUP_VARS, *_ISOLATION_VARS, "OPENCODE_DISABLE_PROJECT_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(host.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host.config))
    monkeypatch.setenv("XDG_DATA_HOME", str(host.data))
    monkeypatch.setenv("XDG_STATE_HOME", str(host.state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(host.cache))
    monkeypatch.setenv("CONCLAVE_HOME", str(host.conclave_home))
    operator = host.operator
    for folder in (host.cache, host.worktree, *vars(operator).values()):
        folder.mkdir(parents=True)
    _write_json(operator.config / "opencode.json", _OPERATOR_CONFIG)
    _write_json(operator.data / "auth.json", _OPERATOR_CREDENTIALS)
    _write_json(operator.data / "sessions" / "ses_operator.json", {"id": "ses_operator"})
    (operator.data / "opencode.db").write_bytes(b"the operator's sessions")
    _write_json(operator.state / "model.json", {"recent": ["llamacpp/qwen3-coder"]})
    (host.config / "git").mkdir()
    (host.config / "git" / "config").write_text("# the operator's git config\n")
    claude = host.home / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("The operator's own instructions.\n")
    (claude / "settings.json").write_text("{}\n")
    (host.home / "claude-skills").mkdir()
    (claude / "skills").symlink_to("../claude-skills")

    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(
        f"#!{sys.executable}\n"
        + _FAKE_OPENCODE.replace("__REPORT__", repr(str(host.report)))
        .replace("__CHILD__", repr(str(host.child)))
        .replace("__COMMAND__", repr(_COMMAND)),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(fake))
    return host


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _behind_symlink(path: Path, target: Path, *, absolute: bool) -> None:
    """Move *path* to *target* and leave a symlink to it at *path*, as a dotfiles manager does
    (stow links relatively, home-manager absolutely)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    path.rename(target)
    path.symlink_to(target if absolute else os.path.relpath(target, path.parent))


def _dotfiles(host: _Host, layout: str) -> None:
    """Rearrange the operator's files the way *layout* names (see its uses)."""

    def link(path: Path, *, absolute: bool) -> None:
        target = host.root / "dotfiles" / path.relative_to(host.root)
        _behind_symlink(path, target, absolute=absolute)

    folders = (*vars(host.operator).values(), host.home / ".claude")
    if layout == "stow":  # each folder a relative link into a dotfiles checkout
        for path in folders:
            link(path, absolute=False)
    elif layout == "absolute-links":  # the same with absolute links; opencode ran on the config
        (host.operator.config / ".gitignore").write_text("node_modules\n")
        for path in folders:
            link(path, absolute=True)
    elif layout == "linked-files":  # home-manager: real folders whose files link elsewhere
        claude = host.home / ".claude"
        for path in (
            host.operator.config / "opencode.json",
            host.operator.data / "auth.json",
            claude / "CLAUDE.md",
            claude / "settings.json",
        ):
            link(path, absolute=True)
    elif layout == "linked-parents":  # a linked home (/home -> var/home) and XDG base folders
        for path in (host.home, host.config, host.data, host.state):
            link(path, absolute=path != host.home)
    else:
        raise AssertionError(layout)


async def _dispatch(
    provider: OpenCodeCliProvider,
    cwd: Path,
    *,
    model: str | None = "deepseek/deepseek-chat",
    sandbox: WorkerSandbox = WorkerSandbox.auto,
    isolated: bool = True,
    extra_env: dict[str, str] | None = None,
    resume: str | None = None,
    prompt: str = "hi",
    cancel_event: asyncio.Event | None = None,
) -> AgentResult:
    profile = ResolvedProfile(
        name="opencode",
        arg_mode=ArgMode.inherit,
        model=model,
        isolated=isolated,
        sandbox=sandbox,
        extra_env=extra_env or {},
        resume_session_id=resume,
    )
    return await provider.run_agent(
        profile=profile, prompt=prompt, timeout_seconds=60, cwd=cwd, cancel_event=cancel_event
    )


def _sandbox_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER
        and record.levelno == logging.WARNING
        and "worker sandbox" in record.getMessage()
    ]


# --- a sandboxed dispatch ------------------------------------------------------------------------


@needs_bwrap
@pytest.mark.parametrize("gitignore", [False, True], ids=["fresh-config-folder", "gitignore"])
async def test_the_operators_opencode_files_are_untouched_and_sessions_stay_with_conclave(
    host: _Host, gitignore: bool
) -> None:
    if gitignore:  # opencode has run on the operator's config folder (else the fake writes one)
        (host.operator.config / ".gitignore").write_text("node_modules\n")
    before = host.operator_snapshot()
    provider = OpenCodeCliProvider()

    first = await _dispatch(provider, host.worktree)
    second = await _dispatch(provider, host.worktree, model=None)  # the operator's default model

    assert first.ok and second.ok, (first.error, second.error)
    assert host.operator_snapshot() == before  # discovery included
    assert sorted(p.name for p in (host.operator.data / "sessions").iterdir()) == [
        "ses_operator.json"
    ]
    assert (host.own("deepseek").data / "sessions" / f"{first.session_id}.json").is_file()
    assert (host.own("llamacpp").data / "sessions" / f"{second.session_id}.json").is_file()
    discovery = host.conclave_home / "opencode-homes" / ".discovery"
    assert (discovery / "data" / "log" / "opencode.log").is_file()
    [operator_discovery] = [d for d in host.reports("debug") if d["cwd"] == "/"]
    assert "OPENCODE_DB" not in operator_discovery["env"]


@needs_bwrap
@pytest.mark.parametrize("layout", ["stow", "absolute-links", "linked-files", "linked-parents"])
async def test_symlinked_operator_folders_are_sandboxed_where_they_lead(
    host: _Host, layout: str
) -> None:
    # bubblewrap refuses to mount onto a symlink: each mount names where the symlink leads.
    _dotfiles(host, layout)
    before = host.operator_snapshot()
    provider = OpenCodeCliProvider()

    first = await _dispatch(provider, host.worktree)
    second = await _dispatch(provider, host.worktree, model=None)  # the operator's default model

    assert first.ok and second.ok, (first.error, second.error)
    assert host.operator_snapshot() == before  # the links and what they lead to, discovery too
    deepseek, llamacpp = host.reports("run")
    assert deepseek["config"] == {"provider": {"deepseek": _DEEPSEEK_BLOCK}}
    assert deepseek["credentials"] == {"deepseek": _OPERATOR_CREDENTIALS["deepseek"]}
    assert llamacpp["argv"][-2:] == ["--model", "llamacpp/qwen3-coder"]
    assert llamacpp["config"] == {"provider": {"llamacpp": _LLAMACPP_BLOCK}}
    assert llamacpp["credentials"] == {}
    for run in (deepseek, llamacpp):
        assert run["claude_md"] is False
        assert run["command"]["claude"].split() == _CLAUDE_ENTRIES
        assert run["command"]["git"] == "# the operator's git config"
        assert run["command"]["HOME"] == str(host.home)  # the same values, links and all
    assert (host.own("deepseek").data / "sessions" / f"{first.session_id}.json").is_file()
    assert (host.own("llamacpp").data / "sessions" / f"{second.session_id}.json").is_file()
    assert (host.home / ".claude" / "CLAUDE.md").read_text() == "The operator's own instructions.\n"


@needs_bwrap
async def test_a_second_dispatch_of_the_same_provider_sees_the_first_ones_session(
    host: _Host,
) -> None:
    provider = OpenCodeCliProvider()

    first = await _dispatch(provider, host.worktree)
    resumed = await _dispatch(provider, host.worktree, resume=first.session_id)
    elsewhere = await _dispatch(
        provider, host.worktree, model="openrouter/qwen/qwen3", resume=first.session_id
    )

    assert first.ok and resumed.ok and elsewhere.ok
    runs = host.reports("run")
    assert [run["session_known"] for run in runs] == [False, True, False]
    assert runs[1]["argv"][-2:] == ["--session", first.session_id]


@needs_bwrap
async def test_concurrent_dispatches_of_two_providers_each_see_only_their_own_credential(
    host: _Host,
) -> None:
    provider = OpenCodeCliProvider()

    results = await asyncio.gather(
        _dispatch(provider, host.worktree, model="deepseek/deepseek-chat"),
        _dispatch(provider, host.worktree, model="openrouter/qwen/qwen3"),
    )

    assert all(result.ok for result in results)
    seen = {run["argv"][run["argv"].index("--model") + 1]: run for run in host.reports("run")}
    assert seen["deepseek/deepseek-chat"]["credentials"] == {
        "deepseek": _OPERATOR_CREDENTIALS["deepseek"]
    }
    assert seen["openrouter/qwen/qwen3"]["credentials"] == {
        "openrouter": _OPERATOR_CREDENTIALS["openrouter"]
    }
    assert seen["openrouter/qwen/qwen3"]["config"] == {
        "provider": {"openrouter": {"options": {"headers": {}}}}  # the redacted header left out
    }
    # Both waited for one reading of the operator's config.
    assert [d["cwd"] for d in host.reports("debug")].count("/") == 1


@needs_bwrap
async def test_the_worker_sees_only_its_providers_block_and_credential(host: _Host) -> None:
    result = await _dispatch(OpenCodeCliProvider(), host.worktree)

    assert result.ok, result.error
    [run] = host.reports("run")
    # No default model, permission rule, MCP server, instruction or other provider.
    assert run["config"] == {"provider": {"deepseek": _DEEPSEEK_BLOCK}}
    assert run["credentials"] == {"deepseek": {"type": "api", "key": "sk-deepseek"}}
    assert run["claude_md"] is False
    assert run["files"]["config"] == [".gitignore", "opencode.json"]  # its own opencode wrote
    assert "auth.json" in run["files"]["data"]
    assert run["argv"][:6] == ["run", "--format", "json", "--auto", "--title", "conclave"]
    assert run["argv"][6:] == ["--dir", str(host.worktree), "--model", "deepseek/deepseek-chat"]
    env = run["env"]
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == env["OPENCODE_DISABLE_EXTERNAL_SKILLS"]
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    assert "OPENCODE_CONFIG_CONTENT" not in env  # no MCP server in the worker's config
    own = host.own("deepseek")
    for path in (own.config / "opencode.json", own.data / "auth.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(own.config.parent.stat().st_mode) == 0o700


@needs_bwrap
async def test_the_providers_config_file_opencode_completed_is_not_rewritten(host: _Host) -> None:
    provider = OpenCodeCliProvider()
    config_file = host.own("deepseek").config / "opencode.json"

    assert (await _dispatch(provider, host.worktree)).ok
    # The worker's opencode wrote the $schema it lacked back into it.
    completed = (config_file.stat().st_ino, config_file.read_bytes())
    assert json.loads(completed[1]) == {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"deepseek": _DEEPSEEK_BLOCK},
    }
    assert (await _dispatch(provider, host.worktree)).ok

    assert (config_file.stat().st_ino, config_file.read_bytes()) == completed


@needs_bwrap
async def test_the_repositorys_own_mcp_servers_are_discovered_in_the_sandbox_and_disabled(
    host: _Host,
) -> None:
    _write_json(
        host.worktree / "opencode.json",
        {"mcp": {"repo-docs": {"type": "remote", "url": "https://docs.invalid/mcp"}}},
    )
    provider = OpenCodeCliProvider()

    for _ in range(2):
        assert (await _dispatch(provider, host.worktree)).ok

    runs = host.reports("run")
    assert json.loads(runs[0]["env"]["OPENCODE_CONFIG_CONTENT"]) == {
        "mcp": {"repo-docs": {"enabled": False}}  # not the operator's playwright
    }
    assert runs[0]["config"]["mcp"] == {
        "repo-docs": {"type": "remote", "url": "https://docs.invalid/mcp", "enabled": False}
    }
    # One discovery of the operator's config (cwd /) and one of the worktree's, each cached.
    discoveries = host.reports("debug")
    assert sorted(d["cwd"] for d in discoveries) == sorted(["/", str(host.worktree)])
    [worktree_discovery] = [d for d in discoveries if d["cwd"] == str(host.worktree)]
    assert not set(_ISOLATION_VARS) & set(worktree_discovery["env"])


@needs_bwrap
async def test_a_local_provider_gets_its_block_and_no_credential(host: _Host) -> None:
    result = await _dispatch(OpenCodeCliProvider(), host.worktree, model="llamacpp/qwen3-coder")

    assert result.ok, result.error
    [run] = host.reports("run")
    assert run["config"] == {"provider": {"llamacpp": _LLAMACPP_BLOCK}}
    assert run["credentials"] == {}
    assert not (host.own("llamacpp").data / "auth.json").exists()


@needs_bwrap
async def test_an_oauth_credential_is_refused_before_anything_starts(host: _Host) -> None:
    result = await _dispatch(OpenCodeCliProvider(), host.worktree, model="anthropic/claude-x")

    assert result.ok is False
    error = result.error or ""
    assert "'anthropic'" in error and "OAuth" in error
    assert "API key" in error and "worker_sandbox" in error and "off" in error
    assert not host.report.exists()  # no discovery, no run
    assert not (host.conclave_home / "opencode-homes" / "anthropic").exists()


@needs_bwrap
async def test_without_a_model_the_operators_default_model_is_used(host: _Host) -> None:
    result = await _dispatch(OpenCodeCliProvider(), host.worktree, model="claude-opus-4-8")

    assert result.ok, result.error
    [run] = host.reports("run")
    assert run["argv"][-2:] == ["--model", "llamacpp/qwen3-coder"]
    assert result.model_reported == "llamacpp/qwen3-coder"
    assert run["config"] == {"provider": {"llamacpp": _LLAMACPP_BLOCK}}


@needs_bwrap
async def test_without_any_model_the_dispatch_fails_asking_for_one(host: _Host) -> None:
    config = {key: value for key, value in _OPERATOR_CONFIG.items() if key != "model"}
    _write_json(host.operator.config / "opencode.json", config)

    result = await _dispatch(OpenCodeCliProvider(), host.worktree, model=None)

    assert result.ok is False
    assert "model" in (result.error or "") and "provider/model" in (result.error or "")
    assert host.reports("run") == []


@needs_bwrap
async def test_the_operator_setup_variables_are_dropped_and_extra_env_can_set_them(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra_config = host.root / "extra.json"
    _write_json(extra_config, {"provider": {"deepseek": {"options": {"region": "eu"}}}})
    content = {"deepseek": {"type": "api", "key": "sk-from-auth-content"}}
    monkeypatch.setenv("OPENCODE_CONFIG", str(extra_config))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(host.root / "extra-config-dir"))
    monkeypatch.setenv("OPENCODE_AUTH_CONTENT", json.dumps(content))
    monkeypatch.setenv("OPENCODE_DB", str(host.root / "operator.db"))
    provider = OpenCodeCliProvider()

    plain = await _dispatch(provider, host.worktree)
    overridden = await _dispatch(
        provider,
        host.worktree,
        extra_env={"OPENCODE_DB": "/profile/opencode.db", "OPENCODE_CONFIG_DIR": "/profile"},
    )

    assert plain.ok and overridden.ok
    first, second = host.reports("run")
    assert not set(_OPERATOR_SETUP_VARS) & set(first["env"])
    assert first["credentials"] == content  # the daemon's OPENCODE_AUTH_CONTENT, copied
    assert first["config"] == {  # the operator's config includes OPENCODE_CONFIG's file
        "provider": {"deepseek": {"options": {"timeout": 600000, "region": "eu"}}}
    }
    assert second["env"]["OPENCODE_DB"] == "/profile/opencode.db"
    assert second["env"]["OPENCODE_CONFIG_DIR"] == "/profile"
    assert "OPENCODE_CONFIG" not in second["env"]
    # The operator's config is read with the operator's variables, but not its session database.
    [operator_discovery] = [d for d in host.reports("debug") if d["cwd"] == "/"]
    assert operator_discovery["env"]["OPENCODE_CONFIG"] == str(extra_config)
    assert operator_discovery["env"]["OPENCODE_AUTH_CONTENT"] == json.dumps(content)
    assert "OPENCODE_DB" not in operator_discovery["env"]


@needs_bwrap
async def test_a_rotated_key_reaches_the_next_dispatch(host: _Host) -> None:
    provider = OpenCodeCliProvider()
    assert (await _dispatch(provider, host.worktree)).ok
    rotated = {**_OPERATOR_CREDENTIALS, "deepseek": {"type": "api", "key": "sk-rotated"}}
    _write_json(host.operator.data / "auth.json", rotated)

    assert (await _dispatch(provider, host.worktree)).ok

    first, second = host.reports("run")
    assert first["credentials"]["deepseek"]["key"] == "sk-deepseek"
    assert second["credentials"]["deepseek"]["key"] == "sk-rotated"
    # The operator's config is read once per binary for the provider's lifetime.
    assert [d["cwd"] for d in host.reports("debug")].count("/") == 1


@needs_bwrap
async def test_a_command_the_worker_runs_sees_the_same_paths(host: _Host) -> None:
    assert (await _dispatch(OpenCodeCliProvider(), host.worktree)).ok

    [run] = host.reports("run")
    command = run["command"]
    assert {var: command[var] for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                          "XDG_STATE_HOME", "XDG_CACHE_HOME", "HOME")} == {
        "XDG_CONFIG_HOME": str(host.config),
        "XDG_DATA_HOME": str(host.data),
        "XDG_STATE_HOME": str(host.state),
        "XDG_CACHE_HOME": str(host.cache),
        "HOME": str(host.home),
    }
    assert command["git"] == "# the operator's git config"
    # ~/.claude keeps every entry, its symlink included, but CLAUDE.md.
    assert command["claude"].split() == _CLAUDE_ENTRIES
    assert (host.home / ".claude" / "CLAUDE.md").read_text() == "The operator's own instructions.\n"


def _alive(pid: int) -> bool:
    """Whether *pid* runs (a zombie waiting to be reaped counts as ended)."""
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat", encoding="utf-8") as status:
            return status.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


@needs_bwrap
async def test_cancelling_a_sandboxed_dispatch_leaves_no_process_behind(host: _Host) -> None:
    cancel = asyncio.Event()
    dispatch = asyncio.create_task(
        _dispatch(OpenCodeCliProvider(), host.worktree, prompt="SPAWN-CHILD", cancel_event=cancel)
    )
    text = await wait_until(
        lambda: host.child.exists() and host.child.read_text(), "the fake never started its child"
    )
    pids = [int(pid) for pid in text.split()]

    cancel.set()
    result = await dispatch

    assert result.cancelled is True
    await wait_until(
        lambda: not any(_alive(pid) for pid in pids), "a process of the dispatch was left behind"
    )


# --- with a stand-in bubblewrap, or none ----------------------------------------------------------


@pytest.fixture
def no_bwrap(host: _Host, monkeypatch: pytest.MonkeyPatch) -> str:
    missing = str(host.root / "no-bwrap")
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", missing)
    return missing


def _capture_spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of every process started, then start it."""
    spawns: list[list[str]] = []
    spawn = asyncio.create_subprocess_exec

    async def capturing(program: Any, *args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        spawns.append([str(arg) for arg in (program, *args)])
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing)
    return spawns


async def test_a_sandbox_bubblewrap_cannot_set_up_fails_at_once_naming_the_fix(
    host: _Host, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A bubblewrap that passes the probe but cannot mount: the dispatch fails with its message
    # and the fix, without the transient retries a run that printed nothing would get.
    calls = host.root / "bwrap-calls.txt"
    bwrap = host.root / "bwrap"
    bwrap.write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> '{calls}'\n"
        'if [ "$*" = "--dev-bind / / -- true" ]; then exit 0; fi\n'
        "echo \"bwrap: Can't mount on symlink destination /dotfiles/opencode\" >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    bwrap.chmod(0o755)
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", str(bwrap))
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)

    result = await _dispatch(OpenCodeCliProvider(), host.worktree)

    assert result.ok is False
    error = result.error or ""
    assert "bwrap: Can't mount on symlink destination /dotfiles/opencode" in error
    assert "worker_sandbox to off" in error
    assert not host.report.exists()  # opencode never started
    started = calls.read_text().splitlines()
    assert sum(" run --format json " in line for line in started) == 1  # not retried
    # The discoveries in the sandbox failed the same way, each named in a warning.
    assert sum("Can't mount on symlink" in message for message in caplog.messages) == 2


async def test_required_without_bubblewrap_fails_and_starts_nothing(
    host: _Host, no_bwrap: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _capture_spawns(monkeypatch)

    result = await _dispatch(OpenCodeCliProvider(), host.worktree, sandbox=WorkerSandbox.required)

    assert result.ok is False
    error = result.error or ""
    assert no_bwrap in error  # the cause
    assert "install bubblewrap" in error and "unprivileged user namespaces" in error
    assert "execution.worker_sandbox to auto or off" in error
    assert spawns == [] and not host.report.exists()


async def test_auto_without_bubblewrap_runs_as_without_the_sandbox_with_one_warning(
    host: _Host, no_bwrap: str, caplog: pytest.LogCaptureFixture
) -> None:
    provider = OpenCodeCliProvider()

    unsandboxed = await _dispatch(provider, host.worktree, sandbox=WorkerSandbox.off)
    fallbacks = [await _dispatch(provider, host.worktree) for _ in range(2)]

    assert unsandboxed.ok and all(result.ok for result in fallbacks)
    off, *auto = host.reports("run")
    for run in auto:
        assert (run["argv"], run["env"], run["config"]) == (off["argv"], off["env"], off["config"])
    assert off["credentials"] == _OPERATOR_CREDENTIALS  # the operator's own, as before
    [warning] = _sandbox_warnings(caplog)
    assert no_bwrap in warning and "install bubblewrap" in warning
    assert "worker_sandbox to off" in warning


@pytest.mark.parametrize(
    ("isolated", "sandbox"),
    [(True, WorkerSandbox.off), (False, WorkerSandbox.required), (False, WorkerSandbox.auto)],
)
async def test_off_and_unisolated_dispatches_run_as_today(
    host: _Host,
    no_bwrap: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolated: bool,
    sandbox: WorkerSandbox,
) -> None:
    spawns = _capture_spawns(monkeypatch)
    probes: list[str] = []
    monkeypatch.setattr(opencode_cli, "sandbox_status", lambda: probes.append("probe"))

    result = await _dispatch(
        OpenCodeCliProvider(), host.worktree, isolated=isolated, sandbox=sandbox
    )

    assert result.ok, result.error
    assert probes == []  # the sandbox is not even looked for
    assert all(Path(argv[0]).name == "opencode" for argv in spawns)
    [run] = host.reports("run")
    expected = {**os.environ}
    if isolated:
        expected |= dict.fromkeys(_ISOLATION_VARS[:2], "1")
        expected["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {"mcp": {"playwright": {"enabled": False}}}
        )
    assert run["env"] == expected
    assert _sandbox_warnings(caplog) == []


# --- the parts, without a sandbox ----------------------------------------------------------------


def test_bwrap_command_binds_the_host_and_ends_with_the_parent() -> None:
    assert bwrap_command("/usr/bin/bwrap", ["--bind", "/a", "/b"]) == (
        "/usr/bin/bwrap", "--dev-bind", "/", "/", "--bind", "/a", "/b", "--die-with-parent", "--",
    )


def test_setup_failure_names_bubblewraps_message_and_the_fix() -> None:
    output = "\n  bwrap: Can't mkdir parents for /x/opencode: Permission denied\n"

    message = setup_failure(output) or ""

    assert "bwrap: Can't mkdir parents for /x/opencode: Permission denied" in message
    assert "worker_sandbox to off" in message
    assert setup_failure('{"type": "text"}\nbwrap: a later line\n') is None
    assert setup_failure("") is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"HOME": "/h"},
            ("/h/.config/opencode", "/h/.local/share/opencode", "/h/.local/state/opencode"),
        ),
        (
            {"HOME": "/h", "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d", "XDG_STATE_HOME": "/s"},
            ("/c/opencode", "/d/opencode", "/s/opencode"),
        ),
        (  # an empty value counts as unset (xdg-basedir)
            {"HOME": "/h", "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "/d"},
            ("/h/.config/opencode", "/d/opencode", "/h/.local/state/opencode"),
        ),
    ],
    ids=["home", "xdg", "empty-xdg"],
)
def test_opencode_folders_resolve_as_opencode_does(
    env: dict[str, str], expected: tuple[str, str, str]
) -> None:
    folders = opencode_folders(env)
    assert (str(folders.config), str(folders.data), str(folders.state)) == expected


def test_conclaves_opencode_homes_follow_the_daemons_home() -> None:
    assert opencode_homes({"CONCLAVE_HOME": "/srv/c", "HOME": "/h"}) == Path(
        "/srv/c/opencode-homes"
    )
    assert opencode_homes({"HOME": "/h"}) == Path("/h/.local/share/conclave/opencode-homes")
    own = provider_folders(Path("/srv/c/opencode-homes"), "deepseek")
    assert (own.config, own.data, own.state) == (
        Path("/srv/c/opencode-homes/deepseek/config"),
        Path("/srv/c/opencode-homes/deepseek/data"),
        Path("/srv/c/opencode-homes/deepseek/state"),
    )


def test_provider_folder_names_are_safe_and_one_to_one() -> None:
    names = ["deepseek", "llama.cpp", "a b", "a_b", "a%20b", ".discovery", ".", "..", "ü", "a:b"]
    folders = [provider_folder_name(name) for name in names]
    assert folders[:2] == ["deepseek", "llama.cpp"]
    assert len(set(folders)) == len(names)
    assert all("/" not in f and not f.startswith(".") and f not in ("", ".", "..") for f in folders)
    with pytest.raises(ValueError):
        provider_folder_name("")


def test_dispatch_mounts_hide_claude_md_and_put_the_providers_folders_over_the_operators(
    tmp_path: Path,
) -> None:
    tmp_path = tmp_path.resolve()  # the expected mounts name resolved paths
    operator = OpencodeFolders(Path("/o/c"), Path("/o/d"), Path("/o/s"))
    own = OpencodeFolders(Path("/h/c"), Path("/h/d"), Path("/h/s"))
    claude = tmp_path / ".claude"
    claude.mkdir(mode=0o700)
    (claude / "settings.json").write_text("{}")
    (claude / "projects").mkdir()
    (claude / "skills").symlink_to("../shared-skills")
    binds = [
        "--bind", "/h/c", "/o/c", "--bind", "/h/d", "/o/d", "--bind", "/h/s", "/o/s",
    ]

    assert dispatch_mounts(operator, own, tmp_path) == binds  # no CLAUDE.md: nothing to hide

    (claude / "CLAUDE.md").write_text("instructions")
    assert dispatch_mounts(operator, own, tmp_path) == [
        "--perms", "0700", "--tmpfs", str(claude),
        "--bind-try", str(claude / "projects"), str(claude / "projects"),
        "--bind-try", str(claude / "settings.json"), str(claude / "settings.json"),
        "--symlink", "../shared-skills", str(claude / "skills"),
        *binds,  # last: what the sandbox shows, even where a link puts one inside .claude
    ]
    assert hidden_file_mounts(tmp_path / "missing" / "CLAUDE.md") == []


def test_mounts_name_where_symlinked_folders_lead(tmp_path: Path) -> None:
    # bubblewrap refuses to mount onto a symlink ("Can't mount on symlink destination"), as a
    # dotfiles setup makes ~/.config/opencode or ~/.claude: every mount names the link's target.
    tmp_path = tmp_path.resolve()
    dotfiles = tmp_path / "dotfiles"
    for name in ("config", "data", "state", "claude", "conclave"):
        (dotfiles / name).mkdir(parents=True, mode=0o700)
    (dotfiles / "claude").chmod(0o700)
    (dotfiles / "claude" / "CLAUDE.md").write_text("instructions")
    (dotfiles / "notes.md").write_text("notes")
    (dotfiles / "claude" / "notes.md").symlink_to(dotfiles / "notes.md")
    links = tmp_path / "links"
    links.mkdir()
    for name in ("config", "data", "state"):
        (links / name).symlink_to(Path("..", "dotfiles", name))  # relative, as stow links
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").symlink_to(dotfiles / "claude")  # absolute, as home-manager links
    (tmp_path / "conclave").symlink_to("dotfiles/conclave")
    operator = OpencodeFolders(links / "config", links / "data", links / "state")
    own = provider_folders(tmp_path / "conclave" / "opencode-homes", "deepseek")
    real_own = provider_folders(dotfiles / "conclave" / "opencode-homes", "deepseek")
    for folder in vars(own).values():
        folder.mkdir(parents=True)
    claude = dotfiles / "claude"

    assert dispatch_mounts(operator, own, home) == [
        "--perms", "0700", "--tmpfs", str(claude),
        "--symlink", str(dotfiles / "notes.md"), str(claude / "notes.md"),
        "--bind", str(real_own.config), str(dotfiles / "config"),
        "--bind", str(real_own.data), str(dotfiles / "data"),
        "--bind", str(real_own.state), str(dotfiles / "state"),
    ]
    discovery = tmp_path / "conclave" / "opencode-homes" / ".discovery"
    (dotfiles / "config" / "opencode.json").symlink_to(dotfiles / "opencode.json")
    (dotfiles / "opencode.json").write_text("{}")
    real_discovery = dotfiles / "conclave" / "opencode-homes" / ".discovery"
    assert discovery_mounts(operator, discovery) == [
        "--perms", "0700", "--tmpfs", str(dotfiles / "config"),
        # A linked entry is mounted from where it leads, at its own path.
        "--ro-bind-try",
        str(dotfiles / "opencode.json"),
        str(dotfiles / "config" / "opencode.json"),
        "--bind", str(real_discovery / "data"), str(dotfiles / "data"),
        "--bind", str(real_discovery / "state"), str(dotfiles / "state"),
    ]


def test_discovery_mounts_read_the_operators_config_folder_read_only(tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()  # the expected mounts name resolved paths
    config = tmp_path / "c"
    operator = OpencodeFolders(config, Path("/o/d"), Path("/o/s"))
    discovery = tmp_path / ".discovery"
    rest = [
        "--bind", str(discovery / "data"), "/o/d", "--bind", str(discovery / "state"), "/o/s",
    ]

    # opencode writes a .gitignore into a config folder without one: a tmpfs takes that write.
    assert discovery_mounts(operator, discovery) == ["--tmpfs", str(config), *rest]
    config.mkdir(mode=0o750)
    config.chmod(0o750)
    (config / "opencode.json").write_text("{}")
    (config / "agent").mkdir()
    (config / "linked.json").symlink_to("opencode.json")
    assert discovery_mounts(operator, discovery) == [
        "--perms", "0750", "--tmpfs", str(config),
        "--ro-bind-try", str(config / "agent"), str(config / "agent"),
        "--ro-bind-try", str(config / "opencode.json"), str(config / "linked.json"),
        "--ro-bind-try", str(config / "opencode.json"), str(config / "opencode.json"),
        *rest,
    ]
    (config / ".gitignore").write_text("node_modules\n")  # opencode has run on it
    assert discovery_mounts(operator, discovery) == [
        "--ro-bind", str(config), str(config), *rest,
    ]


def test_operator_credentials_are_read_as_opencode_reads_them(tmp_path: Path) -> None:
    _write_json(tmp_path / "auth.json", {"a": {"type": "api", "key": "file"}})
    content = json.dumps({"a": {"type": "api", "key": "env"}})

    assert operator_credentials({"OPENCODE_AUTH_CONTENT": content}, tmp_path)["a"]["key"] == "env"
    assert operator_credentials({"OPENCODE_AUTH_CONTENT": "{not json"}, tmp_path)["a"]["key"] == (
        "file"
    )
    assert operator_credentials({"OPENCODE_AUTH_CONTENT": ""}, tmp_path)["a"]["key"] == "file"
    assert operator_credentials({"OPENCODE_AUTH_CONTENT": "[1]"}, tmp_path) == {}
    assert operator_credentials({}, tmp_path / "missing") == {}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"type": "api", "key": "k"}, {"type": "api", "key": "k"}),
        ({"type": "wellknown", "key": "K", "token": "t"}, {"type": "wellknown", "key": "K",
                                                            "token": "t"}),
        ({"type": "unknown"}, None),
        ("not an object", None),
        (None, None),
    ],
)
def test_a_provider_gets_its_api_or_wellknown_credential_only(entry: Any, expected: Any) -> None:
    credentials = {"other": {"type": "api", "key": "x"}}
    if entry is not None:
        credentials["p"] = entry
    assert provider_credential("p", credentials) == expected


def test_an_oauth_credential_is_never_copied() -> None:
    oauth = {"type": "oauth", "refresh": "r", "access": "a", "expires": 0}
    with pytest.raises(SandboxError, match=r"'anthropic'.*OAuth.*API key.*worker_sandbox"):
        provider_credential("anthropic", {"anthropic": oauth})


def test_provider_folders_are_private_and_hold_only_the_providers_files(tmp_path: Path) -> None:
    own = provider_folders(tmp_path / "opencode-homes", "deepseek")
    block = {"options": {"timeout": 1}}
    credential = {"type": "api", "key": "k"}

    prepare_provider_folders(own, "deepseek", block, credential)

    assert json.loads((own.config / "opencode.json").read_text()) == {
        "provider": {"deepseek": block}
    }
    assert json.loads((own.data / "auth.json").read_text()) == {"deepseek": credential}
    for folder in (tmp_path / "opencode-homes", own.config.parent, own.config, own.data,
                   own.state):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for path in (own.config / "opencode.json", own.data / "auth.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in own.config.iterdir()) == ["opencode.json"]  # no temporary

    prepare_provider_folders(own, "deepseek", None, None)  # refreshed: nothing left to hold

    assert list(own.config.iterdir()) == [] and list(own.data.iterdir()) == []


def test_a_current_config_file_is_left_as_opencode_completed_it(tmp_path: Path) -> None:
    own = provider_folders(tmp_path / "opencode-homes", "deepseek")
    block = {"options": {"timeout": 1}}
    prepare_provider_folders(own, "deepseek", block, None)
    config_file = own.config / "opencode.json"
    # opencode writes the $schema a config file lacks back into it, in place.
    schema = {"$schema": "https://opencode.ai/config.json"}
    config_file.write_text(json.dumps({**schema, "provider": {"deepseek": block}}))
    completed = (config_file.stat().st_ino, config_file.read_bytes())

    prepare_provider_folders(own, "deepseek", block, None)

    assert (config_file.stat().st_ino, config_file.read_bytes()) == completed
    # Anything else is replaced: another block, or a file that is no longer private.
    changed = {"options": {"timeout": 2}}
    prepare_provider_folders(own, "deepseek", changed, None)
    assert json.loads(config_file.read_text()) == {"provider": {"deepseek": changed}}
    config_file.write_text(json.dumps({**schema, "provider": {"deepseek": changed}}))
    config_file.chmod(0o644)
    prepare_provider_folders(own, "deepseek", changed, None)
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert json.loads(config_file.read_text()) == {"provider": {"deepseek": changed}}


def test_the_operators_provider_blocks_leave_out_redacted_values() -> None:
    output = json.dumps(
        {
            "model": "deepseek/deepseek-chat",
            "provider": {
                "deepseek": {
                    "options": {"apiKey": "***", "baseURL": "https://api.deepseek.invalid"},
                    "models": {"chat": {"headers": {"X-Key": "***"}, "tags": ["***", 1]}},
                },
                "broken": "not an object",
            },
        }
    )

    config = _operator_config_of(output)

    assert config.model == "deepseek/deepseek-chat"
    assert config.providers == {
        "deepseek": {
            "options": {"baseURL": "https://api.deepseek.invalid"},
            "models": {"chat": {"headers": {}, "tags": ["***", 1]}},
        }
    }
    assert _operator_config_of('{"model": "no-provider"}').model is None
    assert _operator_config_of("{}").providers == {}


# --- availability --------------------------------------------------------------------------------


def _fake_bwrap(path: Path, *, code: int = 0, stderr: str = "") -> Path:
    """A stand-in bubblewrap that counts its runs in ``<path>.runs`` and exits with *code*."""
    path.write_text(
        "#!/bin/sh\n"
        f"echo run >> '{path}.runs'\n"
        + (f"echo '{stderr}' >&2\n" if stderr else "")
        + f"exit {code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _runs_of(path: Path) -> int:
    runs = Path(f"{path}.runs")
    return len(runs.read_text().splitlines()) if runs.exists() else 0


def test_sandbox_status_probes_each_binary_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    works = _fake_bwrap(tmp_path / "bwrap-ok")
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", str(works))

    first, second = sandbox_status(), sandbox_status()

    assert first.available and second.available and first.binary == str(works)
    assert _runs_of(works) == 1


def test_sandbox_status_reports_why_the_sandbox_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = _fake_bwrap(
        tmp_path / "bwrap-denied", code=1, stderr="bwrap: setting up uid map: Permission denied"
    )
    missing = tmp_path / "missing-bwrap"
    reasons: dict[str, Callable[[str], bool]] = {
        str(denied): lambda reason: "exited with 1" in reason and "uid map" in reason,
        str(missing): lambda reason: str(missing) in reason,
    }

    for binary, explains in reasons.items():
        monkeypatch.setenv("CONCLAVE_BWRAP_BIN", binary)  # read at each call
        status = sandbox_status()
        assert status.available is False and explains(status.reason), status
    monkeypatch.delenv("CONCLAVE_BWRAP_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    status = sandbox_status()
    assert status.available is False and "not on PATH" in status.reason
