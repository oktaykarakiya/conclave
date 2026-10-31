"""Tests for the packaged MCP pilot guide, brief, resources, tool, and drift guards."""

from __future__ import annotations

import asyncio
import importlib.resources
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from conclave import __version__
from conclave.db import Database
from conclave.db import repositories as repo
from conclave.mcp.server import handle_mcp_message
from conclave.mcp.tools import (
    MCP_TOOLS,
    PILOT_GUIDE_URI,
    _guide_sections,
    load_pilot_brief,
    load_pilot_guide,
)
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
        return AgentResult(ok=True, text="ok")


async def _call_tool(daemon: Daemon, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    return result


def _text(result: dict[str, Any]) -> str:
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1
    text = content[0]["text"]
    assert isinstance(text, str)
    return text


def test_pilot_files_load_from_package_not_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    guide = (
        importlib.resources.files("conclave.mcp")
        .joinpath("pilot_guide.md")
        .read_text(encoding="utf-8")
    )
    brief = (
        importlib.resources.files("conclave.mcp")
        .joinpath("pilot_brief.md")
        .read_text(encoding="utf-8")
    )
    assert load_pilot_guide() == guide
    assert load_pilot_brief() == brief
    assert guide.startswith("# Conclave Pilot Guide")


async def test_initialize_returns_condensed_model_agnostic_instructions(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    response = await handle_mcp_message(
        daemon, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert response is not None
    instructions = response["result"]["instructions"]
    assert isinstance(instructions, str)
    assert len(instructions) <= 2500
    assert "conclave://guide/pilot" in instructions
    assert "conclave_get_pilot_guide" in instructions
    denied = re.search(
        r"claude|anthropic|deepseek|openai|gpt|gemini|opus|sonnet|haiku|opencode",
        instructions,
        re.IGNORECASE,
    )
    assert denied is None, f"brief names a vendor/model: {denied.group(0)!r}"


async def test_resources_list_and_read_serve_the_pilot_guide(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    project = await repo.create_project(
        db, name="proj", path=str(tmp_path), default_branch="main"
    )

    listed = await handle_mcp_message(
        daemon, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    )
    assert listed is not None
    resources = listed["result"]["resources"]
    assert len(resources) == 2
    assert resources[0]["uri"] == PILOT_GUIDE_URI
    assert resources[0]["name"] == "Conclave Pilot Guide"
    assert resources[0]["mimeType"] == "text/markdown"
    assert resources[1]["uri"] == f"conclave://projects/{project.id}"

    read = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": PILOT_GUIDE_URI},
        },
    )
    assert read is not None
    assert read["result"]["contents"] == [
        {
            "uri": PILOT_GUIDE_URI,
            "mimeType": "text/markdown",
            "text": load_pilot_guide(),
        }
    ]


async def test_get_pilot_guide_returns_markdown_not_json(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    result = await _call_tool(daemon, "conclave_get_pilot_guide", {})
    text = _text(result)
    assert result["isError"] is False
    assert text.startswith("# Conclave Pilot Guide")
    assert "## 12. Tool index" in text
    assert not text.startswith('"')

    guide_tool = next(t for t in MCP_TOOLS if t["name"] == "conclave_get_pilot_guide")
    assert guide_tool["inputSchema"]["properties"]["section"]["type"] == "string"
    assert guide_tool["inputSchema"]["required"] == []


@pytest.mark.parametrize(
    ("query", "heading"),
    [
        ("6", "## 6. Running and monitoring"),
        ("MONITORING", "## 6. Running and monitoring"),
        ("1", "## 1. Mental model"),
        ("12", "## 12. Tool index"),
    ],
)
async def test_get_pilot_guide_section_selection(
    db: Database, tmp_path: Path, query: str, heading: str
) -> None:
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    result = await _call_tool(daemon, "conclave_get_pilot_guide", {"section": query})
    assert result["isError"] is False
    text = _text(result)
    assert text.startswith(heading)
    if query in ("6", "MONITORING"):
        assert "## 7." not in text
    if query == "1":
        assert not text.startswith("## 12.")
    assert "Live environment" in text


async def test_get_pilot_guide_unknown_section_lists_headings(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    result = await _call_tool(daemon, "conclave_get_pilot_guide", {"section": "nonexistent"})
    assert result["isError"] is True
    text = _text(result)
    assert "unknown section" in text
    assert "1. Mental model" in text
    assert "12. Tool index" in text


async def test_get_pilot_guide_live_footer(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONCLAVE_OPENCODE_BIN", str(tmp_path / "missing-opencode"))
    monkeypatch.setenv("CONCLAVE_CLAUDE_BIN", sys.executable)
    daemon = Daemon(db, tmp_path, _StubProvider(), workers_enabled=False)
    text = _text(await _call_tool(daemon, "conclave_get_pilot_guide", {"section": "6"}))
    assert "Live environment" in text
    assert __version__ in text
    assert "Default engine: opencode" in text
    assert "opencode: not found" in text
    assert "claude: available" in text
    assert f"MCP tools: {len(MCP_TOOLS)}" in text


def test_drift_guard_every_mentioned_tool_exists() -> None:
    names = {t["name"] for t in MCP_TOOLS}
    mentioned = set(
        re.findall(r"\bconclave_[a-z_]+\b", load_pilot_guide() + "\n" + load_pilot_brief())
    )
    assert mentioned <= names, f"unknown tools mentioned: {sorted(mentioned - names)}"


def test_drift_guard_every_tool_is_in_the_final_index_section() -> None:
    names = {t["name"] for t in MCP_TOOLS}
    guide = load_pilot_guide()
    headings = [line for line in guide.splitlines() if line.startswith("## ")]
    assert headings[-1] == "## 12. Tool index"
    sections = _guide_sections(guide)
    assert sections[-1][0] == "12"
    indexed = set(re.findall(r"\bconclave_[a-z_]+\b", sections[-1][2]))
    assert names <= indexed, f"missing from tool index: {sorted(names - indexed)}"
