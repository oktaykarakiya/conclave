"""Effort per dispatch: auto, clamping, the opencode variant lookup and the dispatch wiring.

Deterministic: the variant lookup is a stub or a fake ``opencode`` script, never the real CLI.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from conclave.config import load_project_config
from conclave.db import Database, EventRow
from conclave.db import repositories as repo
from conclave.engine import AgentRunner
from conclave.events import EventBus, EventType
from conclave.planning.session import PlanningOrchestrator
from conclave.providers import (
    AgentResult,
    EffortChoice,
    Engine,
    EngineRouter,
    OnChunk,
    ResolvedProfile,
    resolve_dispatch_effort,
)
from conclave.providers.effort import (
    CLAUDE_CODE_AUTO_EFFORT,
    CLAUDE_CODE_EFFORTS,
    OpencodeVariants,
    choose_effort,
    parse_verbose_models,
)

_FLASH = "deepseek/deepseek-flash"
_FLASH_VARIANTS = frozenset({"low", "high", "max"})


# --- the pure rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "supported", "auto", "used", "clamped"),
    [
        # auto (None): the engine's fixed choice, else the highest configurable level
        (None, CLAUDE_CODE_EFFORTS, CLAUDE_CODE_AUTO_EFFORT, "xhigh", False),
        (None, _FLASH_VARIANTS, None, "max", False),
        (None, None, None, None, False),
        (None, frozenset(), None, None, False),
        (None, frozenset({"none", "minimal", "high"}), None, "high", False),
        (None, frozenset({"none", "minimal"}), None, None, False),
        # explicit and supported, unknown support, or a custom variant: unchanged
        ("high", _FLASH_VARIANTS, None, "high", False),
        ("high", None, None, "high", False),
        ("turbo", _FLASH_VARIANTS, None, "turbo", False),
        # explicit and unsupported: the nearest supported level, the lower one on a tie
        ("xhigh", _FLASH_VARIANTS, None, "high", True),
        ("medium", _FLASH_VARIANTS, None, "low", True),
        ("low", frozenset({"high", "max"}), None, "high", True),
        ("medium", frozenset({"minimal", "high"}), None, "high", True),
        ("minimal", CLAUDE_CODE_EFFORTS, CLAUDE_CODE_AUTO_EFFORT, "low", True),
        ("high", frozenset(), None, None, True),
    ],
)
def test_choose_effort(
    requested: str | None,
    supported: frozenset[str] | None,
    auto: str | None,
    used: str | None,
    clamped: bool,
) -> None:
    choice = choose_effort(requested, supported, auto=auto)
    assert choice == EffortChoice(requested=requested, used=used)
    assert choice.clamped is clamped


def test_claude_code_auto_is_xhigh_never_max() -> None:
    assert CLAUDE_CODE_AUTO_EFFORT == "xhigh"
    assert CLAUDE_CODE_EFFORTS == {"low", "medium", "high", "xhigh", "max"}


# --- resolution per engine ------------------------------------------------------


class _Recorder:
    """A sub-provider that records the profile it was dispatched with."""

    def __init__(self) -> None:
        self.profiles: list[ResolvedProfile] = []

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
        self.profiles.append(profile)
        return AgentResult(ok=True, text="done")


class _Variants:
    """A stub variant lookup: fixed variants per model; it can also raise."""

    def __init__(
        self, table: dict[str, frozenset[str]] | None = None, *, error: Exception | None = None
    ) -> None:
        self.table = table or {}
        self.error = error
        self.calls: list[str] = []

    async def lookup(self, model: str) -> frozenset[str] | None:
        self.calls.append(model)
        if self.error is not None:
            raise self.error
        return self.table.get(model)


def _router(
    variants: Any, default: Engine = Engine.opencode
) -> tuple[EngineRouter, _Recorder, _Recorder]:
    oc, cc = _Recorder(), _Recorder()
    router = EngineRouter(
        default,
        opencode=oc,  # type: ignore[arg-type]
        claude=cc,  # type: ignore[arg-type]
        variants=variants,
    )
    return router, oc, cc


async def test_claude_code_auto_resolves_to_xhigh_without_a_lookup() -> None:
    variants = _Variants({_FLASH: _FLASH_VARIANTS})
    router, _, _ = _router(variants)
    assert await resolve_dispatch_effort(router, "claude-code/opus", None) == EffortChoice(
        None, "xhigh"
    )
    assert variants.calls == []


async def test_opencode_auto_resolves_to_the_highest_variant() -> None:
    variants = _Variants({_FLASH: _FLASH_VARIANTS})
    router, _, _ = _router(variants)
    assert await resolve_dispatch_effort(router, _FLASH, None) == EffortChoice(None, "max")
    assert variants.calls == [_FLASH]


@pytest.mark.parametrize(
    "variants",
    [_Variants(), _Variants(error=RuntimeError("lookup broke"))],
    ids=["unknown", "raising"],
)
async def test_unknown_or_failing_variants_give_no_flag(variants: _Variants) -> None:
    router, _, _ = _router(variants)
    assert await resolve_dispatch_effort(router, _FLASH, None) == EffortChoice(None, None)
    # An explicit level is passed through as before when support is unknown.
    assert await resolve_dispatch_effort(router, _FLASH, "high") == EffortChoice("high", "high")


async def test_claude_default_engine_runs_an_opencode_model_on_claude_code() -> None:
    variants = _Variants({_FLASH: _FLASH_VARIANTS})
    router, _, _ = _router(variants, default=Engine.claude)
    assert await resolve_dispatch_effort(router, _FLASH, None) == EffortChoice(None, "xhigh")
    assert variants.calls == []


async def test_a_provider_without_variants_is_never_looked_up() -> None:
    plain = _Recorder()  # like FakeProvider: no `default`, no `variants`
    assert await resolve_dispatch_effort(plain, _FLASH, None) == EffortChoice(None, None)
    assert await resolve_dispatch_effort(plain, _FLASH, "xhigh") == EffortChoice("xhigh", "xhigh")


# --- the opencode variant lookup ----------------------------------------------------


def _verbose_output() -> str:
    """``opencode models deepseek --verbose`` as opencode prints it: header line + JSON."""
    models = {
        "deepseek/deepseek-flash": {
            "id": "deepseek-flash",
            "providerID": "deepseek",
            "variants": {name: {"reasoningEffort": name} for name in ("low", "high", "max")},
        },
        "deepseek/deepseek-chat": {"id": "deepseek-chat", "providerID": "deepseek", "variants": {}},
    }
    return "".join(f"{name}\n{json.dumps(obj, indent=2)}\n" for name, obj in models.items())


def _fake_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, *, counter: Path | None = None
) -> Path:
    """A fake ``opencode`` (a Python script, so it runs on every platform) wired in as the CLI."""
    script = tmp_path / "fake_opencode.py"
    lines = ["#!/usr/bin/env python3", "import sys, time"]
    if counter is not None:
        lines.append(f"open({str(counter)!r}, 'a').write('run\\n')")
    script.write_text("\n".join([*lines, body]) + "\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr("conclave.providers.opencode_cli._OPENCODE_BIN", str(script))
    return script


def test_parse_verbose_models_reads_header_lines_and_json() -> None:
    assert parse_verbose_models(_verbose_output()) == {
        "deepseek/deepseek-flash": frozenset({"low", "high", "max"}),
        "deepseek/deepseek-chat": frozenset(),
    }


async def test_opencode_variants_reads_once_per_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "runs.txt"
    _fake_opencode(
        tmp_path,
        monkeypatch,
        f"assert sys.argv[1:] == ['models', 'deepseek', '--verbose']\n"
        f"sys.stdout.write({_verbose_output()!r})",
        counter=counter,
    )
    lookup = OpencodeVariants()

    first, second = await asyncio.gather(
        lookup.lookup("deepseek/deepseek-flash"), lookup.lookup("deepseek/deepseek-chat")
    )
    assert first == frozenset({"low", "high", "max"})
    assert second == frozenset()
    assert await lookup.lookup("deepseek/deepseek-flash") == frozenset({"low", "high", "max"})
    assert await lookup.lookup("deepseek/not-listed") is None
    assert counter.read_text().splitlines() == ["run"]  # one run for every deepseek lookup


async def test_opencode_variants_timeout_gives_unknown_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_opencode(tmp_path, monkeypatch, "time.sleep(30)")
    lookup = OpencodeVariants(timeout_seconds=0.5)

    started = time.monotonic()
    assert await lookup.lookup(_FLASH) is None
    assert time.monotonic() - started < 10
    assert await lookup.lookup(_FLASH) is None  # cached: no second wait


@pytest.mark.parametrize(
    "body",
    [f"sys.stdout.write({_verbose_output()!r}); sys.exit(3)", "sys.stdout.write('{not json')"],
    ids=["nonzero-exit", "garbage"],
)
async def test_opencode_variants_failures_give_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _fake_opencode(tmp_path, monkeypatch, body)
    assert await OpencodeVariants().lookup(_FLASH) is None


async def test_opencode_variants_missing_binary_gives_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "conclave.providers.opencode_cli._OPENCODE_BIN", str(tmp_path / "missing-opencode")
    )
    lookup = OpencodeVariants()
    assert await lookup.lookup(_FLASH) is None
    assert await lookup.lookup("no-slash") is None


# --- the dispatch wiring --------------------------------------------------------------


async def _run_developer(
    db: Database, tmp_path: Path, router: EngineRouter, config: dict[str, Any] | None = None
) -> tuple[AgentResult, list[EventRow]]:
    project = await repo.create_project(
        db, name="p", path=str(tmp_path), default_branch="main", config=config or {}
    )
    runner = AgentRunner(
        db, EventBus(db), router, project.id, load_project_config(project.config)
    )
    result = await runner.run(agent="developer", prompt="add a feature", worktree=tmp_path)
    return result, await repo.list_events(db, project_id=project.id)


def _payloads(events: list[EventRow], type_: str) -> list[dict[str, Any]]:
    return [e.payload for e in events if e.type == type_]


def _clamps(events: list[EventRow]) -> list[dict[str, Any]]:
    return [p for p in _payloads(events, EventType.log) if p.get("stage") == "effort_clamped"]


async def test_runner_dispatches_auto_as_the_highest_opencode_variant(
    db: Database, tmp_path: Path
) -> None:
    router, oc, _ = _router(_Variants({_FLASH: _FLASH_VARIANTS}))
    result, events = await _run_developer(db, tmp_path, router)

    assert result.ok is True
    assert [p.effort for p in oc.profiles] == ["max"]
    (dispatched,) = _payloads(events, EventType.agent_dispatched)
    assert (dispatched["model"], dispatched["effort"]) == (_FLASH, "max")
    assert _clamps(events) == []


async def test_runner_clamps_an_unsupported_effort_with_one_event(
    db: Database, tmp_path: Path
) -> None:
    router, oc, _ = _router(_Variants({_FLASH: _FLASH_VARIANTS}))
    config = {"agent_overrides": {"developer": {"effort": "xhigh"}}}
    result, events = await _run_developer(db, tmp_path, router, config)

    assert result.ok is True
    assert [p.effort for p in oc.profiles] == ["high"]
    (clamp,) = _clamps(events)
    assert {k: clamp[k] for k in ("requested", "used", "model", "agent", "node_id")} == {
        "requested": "xhigh",
        "used": "high",
        "model": _FLASH,
        "agent": "developer",
        "node_id": "developer",
    }
    assert "xhigh" in clamp["message"] and "high" in clamp["message"]
    assert _payloads(events, EventType.agent_dispatched)[0]["effort"] == "high"


async def test_runner_dispatches_claude_code_auto_as_xhigh(
    db: Database, tmp_path: Path
) -> None:
    router, oc, cc = _router(_Variants())
    config = {"agent_defaults": {"model": "claude-code/opus"}}
    result, events = await _run_developer(db, tmp_path, router, config)

    assert result.ok is True
    assert oc.profiles == []
    assert [(p.model, p.effort) for p in cc.profiles] == [("opus", "xhigh")]
    assert _payloads(events, EventType.agent_dispatched)[0]["effort"] == "xhigh"


async def test_runner_survives_a_raising_lookup(db: Database, tmp_path: Path) -> None:
    router, oc, _ = _router(_Variants(error=OSError("no opencode")))
    result, events = await _run_developer(db, tmp_path, router)

    assert result.ok is True
    assert [p.effort for p in oc.profiles] == [None]
    assert _payloads(events, EventType.agent_dispatched)[0]["effort"] is None
    assert _clamps(events) == []


async def test_runner_survives_a_timed_out_lookup(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_opencode(tmp_path, monkeypatch, "time.sleep(30)")
    router, oc, _ = _router(OpencodeVariants(timeout_seconds=0.5))
    result, events = await _run_developer(db, tmp_path, router)

    assert result.ok is True
    assert [p.effort for p in oc.profiles] == [None]
    assert _payloads(events, EventType.agent_dispatched)[0]["effort"] is None


async def test_planning_turn_dispatches_claude_code_auto_as_xhigh(
    db: Database, tmp_path: Path
) -> None:
    provider = _Recorder()
    project = await repo.create_project(
        db,
        name="p",
        path=str(tmp_path),
        default_branch="main",
        config={"agent_defaults": {"model": "claude-code/opus"}},
    )
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Plan X"
    )
    orchestrator = PlanningOrchestrator(db, EventBus(db), provider)

    text, _ = await orchestrator._agent_turn(session.id, "tester", "SYSTEM", "Review the plan")

    assert text == "done"
    assert [(p.model, p.effort) for p in provider.profiles] == [("claude-code/opus", "xhigh")]
