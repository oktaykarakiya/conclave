"""Unit tests for the ported engine logic: verdicts, grounding, pipeline, memory."""

from __future__ import annotations

from pathlib import Path

from conclave.config import AgentsPolicy
from conclave.engine import (
    AttemptMemory,
    build_baseline_preamble,
    check_grounding,
    get_agent_pipeline,
    parse_verdict,
    trim_output,
)

# --- parse_verdict ----------------------------------------------------------


def test_parse_json_pass() -> None:
    v = parse_verdict('Some preamble\n```json\n{"verdict": "pass", "reason": "ok"}\n```')
    assert v.verdict == "pass"
    assert v.source == "json"
    assert v.reason == "ok"


def test_parse_json_fail_with_evidence() -> None:
    text = (
        '```json\n{"verdict": "fail", "reason": "bug", '
        '"evidence": [{"file": "a.js", "line": 3}]}\n```'
    )
    v = parse_verdict(text)
    assert v.verdict == "fail"
    assert v.evidence == [{"file": "a.js", "line": 3}]


def test_parse_legacy_strings() -> None:
    assert parse_verdict("VERDICT: PASS").verdict == "pass"
    assert parse_verdict("VERDICT: FAIL because x").verdict == "fail"
    assert parse_verdict("VERDICT: BLOCK").verdict == "block"
    assert parse_verdict("VERDICT: DECLINE edge case").verdict == "decline"


def test_parse_unknown_when_absent_or_invalid() -> None:
    assert parse_verdict("no verdict here").verdict == "unknown"
    assert parse_verdict('```json\n{"verdict": "weird"}\n```').verdict == "unknown"


# --- check_grounding --------------------------------------------------------


def test_grounding_keeps_real_evidence(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    diff = "diff --git a/src/a.py b/src/a.py\n@@\n+x = 1"
    v = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "bad", '
        '"evidence": [{"file": "src/a.py", "line": 1}]}\n```'
    )
    out, warnings = check_grounding(v, diff, tmp_path)
    assert out.verdict == "fail"
    assert warnings == []


def test_grounding_downgrades_when_not_in_diff(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    v = parse_verdict('```json\n{"verdict": "fail", "evidence": [{"file": "a.py"}]}\n```')
    out, warnings = check_grounding(v, "diff --git a/other.py b/other.py", tmp_path)
    assert out.verdict == "unknown"
    assert "DOWNGRADED" in out.reason
    assert any("not in this task's diff" in w for w in warnings)


def test_grounding_downgrades_when_not_on_disk(tmp_path: Path) -> None:
    diff = "diff --git a/ghost.py b/ghost.py\n+x"
    v = parse_verdict('```json\n{"verdict": "decline", "evidence": [{"file": "ghost.py"}]}\n```')
    out, _ = check_grounding(v, diff, tmp_path)
    assert out.verdict == "unknown"


def test_grounding_skips_pass_and_string_source(tmp_path: Path) -> None:
    assert check_grounding(parse_verdict("VERDICT: PASS"), "", tmp_path)[0].verdict == "pass"
    # string-sourced fail cannot be grounded, so it is preserved
    assert check_grounding(parse_verdict("VERDICT: FAIL"), "", tmp_path)[0].verdict == "fail"


# --- pipeline ---------------------------------------------------------------


def test_pipeline_mandatory_only_for_trivial_diff() -> None:
    diff = "diff --git a/README.md b/README.md\n@@\n+hello"
    assert get_agent_pipeline(diff, AgentsPolicy()) == ["tester", "security", "reviewer"]


def test_pipeline_db_migration_adds_specialists() -> None:
    diff = (
        "diff --git a/db/migrations/005_x.sql b/db/migrations/005_x.sql\n"
        "new file mode 100644\n+CREATE TABLE foo (id int);"
    )
    pipeline = set(get_agent_pipeline(diff, AgentsPolicy()))
    assert {"architect", "risk", "performance", "devops"} <= pipeline
    assert "legal" not in pipeline


def test_pipeline_auth_change_adds_legal_and_risk() -> None:
    diff = "diff --git a/auth/login.js b/auth/login.js\n@@\n+const token = jwt.sign(user)"
    pipeline = set(get_agent_pipeline(diff, AgentsPolicy()))
    assert "legal" in pipeline
    assert "risk" in pipeline


# --- memory -----------------------------------------------------------------


def test_attempt_memory_trims_and_renders() -> None:
    mem = AttemptMemory(max_entries=2)
    mem.add(1, "diff-1", "rejected because A")
    mem.add(2, "diff-2", "rejected because B")
    mem.add(3, "diff-3", "rejected because C")
    preamble = mem.build_preamble()
    assert "rejected because A" not in preamble  # trimmed to last 2
    assert "rejected because B" in preamble
    assert "rejected because C" in preamble
    assert "PRIOR ATTEMPT HISTORY" in preamble


def test_attempt_memory_empty() -> None:
    assert AttemptMemory().build_preamble() == ""


# --- baseline ---------------------------------------------------------------


def test_baseline_preamble_and_trim() -> None:
    assert build_baseline_preamble("main", "") == ""
    preamble = build_baseline_preamble("vibes", "FAIL suite-x")
    assert "PRE-EXISTING TEST FAILURES on `vibes`" in preamble
    assert "FAIL suite-x" in preamble

    trimmed = trim_output("\n".join(str(i) for i in range(500)), max_lines=10)
    assert trimmed.splitlines() == [str(i) for i in range(490, 500)]
