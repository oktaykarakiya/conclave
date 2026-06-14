"""Repo onboarding: learn languages, frameworks, and commands from manifests.

The MVP analysis is heuristic and deterministic (no LLM) so it is fast and testable;
an LLM ``repo-analyst`` enrichment pass runs after the heuristic scan to produce a
deeper, AI-informed understanding of the repository.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig
from ..db import Database, Project, RepoKnowledgeRow
from ..db import repositories as repo
from ..engine.gitio import run_git
from ..engine.runner import AgentRunner
from ..events import EventBus, EventType
from ..providers import Provider
from .knowledge import RepoKnowledge, manifest_fingerprint

logger = logging.getLogger("conclave.repo_intel")

_JS_FRAMEWORKS = (
    "react", "vue", "svelte", "next", "nuxt", "express", "fastify", "koa",
    "jest", "vitest", "mocha", "playwright", "@playwright/test",
)

# Match the JSON block the repo-analyst persona is instructed to emit.
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def analyze_repo(repo_path: Path) -> RepoKnowledge:
    languages: list[str] = []
    frameworks: list[str] = []
    commands: dict[str, str] = {}
    conventions: list[str] = []
    protected: list[str] = []

    pkg = repo_path / "package.json"
    if pkg.is_file():
        languages.append("javascript")
        data = _read_json(pkg)
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        if isinstance(scripts, dict):
            if "test" in scripts:
                commands["test"] = "npm test"
            if "build" in scripts:
                commands["build"] = "npm run build"
            if "lint" in scripts:
                commands["lint"] = "npm run lint"
            if "start" in scripts:
                commands["start"] = "npm start"
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        frameworks += [fw for fw in _JS_FRAMEWORKS if fw in deps]
        if data.get("type") == "module":
            conventions.append('ESM modules ("type": "module") — do not emit CommonJS require()')
        if (repo_path / "package-lock.json").is_file():
            protected.append("package-lock.json")
    if (repo_path / "tsconfig.json").is_file():
        languages.append("typescript")

    if (
        (repo_path / "pyproject.toml").is_file()
        or (repo_path / "setup.py").is_file()
        or (repo_path / "requirements.txt").is_file()
    ):
        languages.append("python")
        commands.setdefault("test", "pytest")

    if (repo_path / "Cargo.toml").is_file():
        languages.append("rust")
        commands.setdefault("test", "cargo test")
        commands.setdefault("build", "cargo build")

    if (repo_path / "go.mod").is_file():
        languages.append("go")
        commands.setdefault("test", "go test ./...")

    if (repo_path / "pom.xml").is_file():
        languages.append("java")
        commands.setdefault("test", "mvn -q test")

    if (repo_path / "docker-compose.yml").is_file():
        protected.append("docker-compose.yml")

    layout: dict[str, list[str]] = {}
    for d in ("src", "lib", "app", "tests", "test"):
        if (repo_path / d).is_dir():
            layout.setdefault("dirs", []).append(d)

    languages = list(dict.fromkeys(languages))
    frameworks = list(dict.fromkeys(frameworks))
    stack = ", ".join(languages) if languages else "an unknown stack"
    summary = f"Repository using {stack}."
    return RepoKnowledge(
        languages=languages,
        frameworks=frameworks,
        commands=commands,
        architecture_summary=summary,
        conventions=conventions,
        layout=layout,
        protected_globs=list(dict.fromkeys(protected)),
    )


async def onboard(
    db: Database,
    bus: EventBus,
    project: Project,
    *,
    force: bool = False,
    provider: Provider | None = None,
    config: ConclaveConfig | None = None,
) -> RepoKnowledgeRow:
    """Analyze the repo and persist knowledge, then optionally enrich with AI.

    If ``provider`` and ``config`` are supplied, an AI enrichment pass runs after
    the heuristic scan (unless manifests + SHA are unchanged and the latest knowledge
    is already AI-enriched).
    """
    repo_path = Path(project.path)
    fingerprint = manifest_fingerprint(repo_path)
    code, sha_out = await run_git(repo_path, "rev-parse", "HEAD")
    sha = sha_out.strip() if code == 0 else None

    current = await repo.current_repo_knowledge(db, project.id)
    # Full skip: manifests unchanged AND latest knowledge is already AI-enriched.
    if (
        current is not None
        and not force
        and current.manifest_fingerprint == fingerprint
        and current.ai_enriched
    ):
        return current

    # Heuristic scan (skip if manifests unchanged but AI enrichment is needed).
    if current is not None and not force and current.manifest_fingerprint == fingerprint:
        heuristic_row = current
    else:
        await bus.emit(
            type=EventType.onboarding_started, project_id=project.id, payload={"force": force}
        )
        knowledge = analyze_repo(repo_path)
        heuristic_row = await repo.save_repo_knowledge(
            db,
            project_id=project.id,
            knowledge=knowledge.model_dump(),
            sha=sha,
            manifest_fingerprint=fingerprint,
        )
        await bus.emit(
            type=EventType.onboarding_complete,
            project_id=project.id,
            payload={"languages": knowledge.languages, "commands": knowledge.commands},
        )

    # AI enrichment pass.
    if provider is not None and config is not None:
        ai_knowledge = RepoKnowledge(**heuristic_row.knowledge)
        try:
            enriched = await ai_enrich(
                db, bus, provider, project, config, ai_knowledge, sha, fingerprint
            )
            if enriched is not None:
                return enriched
        except Exception:
            logger.exception("AI enrichment failed for project %s", project.id)

    return heuristic_row


async def ai_enrich(
    db: Database,
    bus: EventBus,
    provider: Provider,
    project: Project,
    config: ConclaveConfig,
    heuristic: RepoKnowledge,
    sha: str | None,
    fingerprint: str,
) -> RepoKnowledgeRow | None:
    """Run the LLM repo-analyst and persist enriched knowledge.

    Returns the new ``RepoKnowledgeRow`` on success, or ``None`` if the AI call
    failed (so the caller falls back to heuristic knowledge).
    """
    repo_path = Path(project.path)

    await bus.emit(
        type=EventType.onboarding_ai_started,
        project_id=project.id,
        payload={"sha": sha},
    )

    prompt = _build_ai_prompt(heuristic)
    runner = AgentRunner(db, bus, provider, project.id, config)

    result = await runner.run(
        agent="repo-analyst",
        prompt=prompt,
        worktree=repo_path,
        task_id=None,
    )

    if not result.ok or not result.text:
        logger.warning(
            "repo-analyst returned ok=%s for project %s: %s",
            result.ok, project.id, result.error or "empty text",
        )
        await bus.emit(
            type=EventType.onboarding_ai_complete,
            project_id=project.id,
            payload={"ok": False, "error": result.error or "empty response"},
        )
        return None

    ai_data = _parse_ai_json(result.text)
    if ai_data is None:
        logger.warning("repo-analyst output had no valid JSON for project %s", project.id)
        await bus.emit(
            type=EventType.onboarding_ai_complete,
            project_id=project.id,
            payload={"ok": False, "error": "no valid JSON in response"},
        )
        return None

    enriched = _merge_knowledge(heuristic, ai_data)
    row = await repo.save_repo_knowledge(
        db,
        project_id=project.id,
        knowledge=enriched.model_dump(),
        sha=sha,
        manifest_fingerprint=fingerprint,
        ai_enriched=True,
    )
    await bus.emit(
        type=EventType.onboarding_ai_complete,
        project_id=project.id,
        payload={
            "ok": True,
            "languages": enriched.languages,
            "commands": enriched.commands,
        },
    )
    return row


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_ai_prompt(heuristic: RepoKnowledge) -> str:
    """Build the user prompt for the repo-analyst agent."""
    lang_text = ", ".join(heuristic.languages) if heuristic.languages else "none detected"
    fw_text = ", ".join(heuristic.frameworks) if heuristic.frameworks else "none detected"
    lines: list[str] = [
        "# Repository Analysis — AI Enrichment Pass",
        "",
        "The deterministic heuristic scan already detected the following:",
        "",
        f"- **Languages**: {lang_text}",
        f"- **Frameworks**: {fw_text}",
    ]
    if heuristic.commands:
        lines.append("- **Commands**:")
        for k, v in heuristic.commands.items():
            lines.append(f"  - `{k}`: `{v}`")
    if heuristic.layout:
        layout_dirs = heuristic.layout.get("dirs", [])
        if layout_dirs:
            lines.append(
                f"- **Layout**: directories found — {', '.join(layout_dirs)}"
            )

    lines += [
        "",
        "Now explore this repository in depth. Read the README, key source files,",
        "configuration files, and directory structure. Understand:",
        "",
        "1. **What does this project do?** — look at the README and main"
        " entry points.",
        "2. **Architecture** — how are modules/components organized?"
        " What patterns are used?",
        "3. **Coding conventions** — what idioms, patterns, or rules does the"
        " codebase follow?",
        "4. **Missing facts** — any languages, frameworks, or commands the"
        " heuristic missed?",
        "5. **Protected files** — any files beyond lockfiles that should never"
        " be auto-edited?",
        "",
        "Output EXACTLY one JSON block with NO text after it:",
        "```json",
        "{",
        '  "languages": ["..."],',
        '  "frameworks": ["..."],',
        '  "commands": {"test": "...", "build": "...", "lint": "...",'
        ' "start": "..."},',
        '  "architecture_summary": "2-4 sentence overview of what this project'
        ' does and how it is structured",',
        '  "conventions": ["specific, actionable convention 1",'
        ' "convention 2"],',
        '  "protected_globs": ["extra-file-pattern"],',
        '  "layout": {"dirs": ["dir1", "dir2"]}',
        "}",
        "```",
    ]
    return "\n".join(lines)


def _parse_ai_json(text: str) -> dict[str, Any] | None:
    """Extract and parse the JSON block from the agent's output."""
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        # Try to find a JSON block bounded by ```json fences.
        fenced = re.search(r"```json\s*([\s\S]*?)\s*```", text)
        if fenced:
            try:
                data = json.loads(fenced.group(1))
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            return None
    return data if isinstance(data, dict) else None


def _merge_knowledge(heuristic: RepoKnowledge, ai_data: dict[str, Any]) -> RepoKnowledge:
    """Merge AI-enriched data onto the heuristic baseline.

    - Factual lists (languages, frameworks, protected_globs): union.
    - Commands: AI overrides when it provides a value.
    - Qualitative fields (architecture_summary, conventions): AI wins.
    - Layout: union of dir lists.
    """
    ai_langs = _str_list(ai_data.get("languages"))
    ai_frameworks = _str_list(ai_data.get("frameworks"))
    _ai_commands_raw = ai_data.get("commands")
    ai_commands = _ai_commands_raw if isinstance(_ai_commands_raw, dict) else {}
    ai_summary = str(ai_data.get("architecture_summary", ""))
    ai_conventions = _str_list(ai_data.get("conventions"))
    ai_globs = _str_list(ai_data.get("protected_globs"))
    ai_layout_dirs = _str_list(ai_data.get("layout", {}).get("dirs"))

    # Union factual fields.
    languages = list(dict.fromkeys(heuristic.languages + ai_langs))
    frameworks = list(dict.fromkeys(heuristic.frameworks + ai_frameworks))
    protected = list(dict.fromkeys(heuristic.protected_globs + ai_globs))

    # AI commands override heuristic entries, heuristic keeps entries AI didn't supply.
    commands = dict(heuristic.commands)
    for k, v in ai_commands.items():
        if isinstance(v, str) and v.strip():
            commands[k] = v.strip()

    # AI wins qualitative fields.
    summary = ai_summary if ai_summary.strip() else heuristic.architecture_summary
    conventions = ai_conventions if ai_conventions else heuristic.conventions

    # Merge layout dirs.
    heuristic_dirs = heuristic.layout.get("dirs", [])
    merged_dirs = list(dict.fromkeys(heuristic_dirs + ai_layout_dirs))
    layout: dict[str, list[str]] = {"dirs": merged_dirs} if merged_dirs else {}

    return RepoKnowledge(
        languages=languages,
        frameworks=frameworks,
        commands=commands,
        architecture_summary=summary,
        conventions=conventions,
        layout=layout,
        protected_globs=protected,
    )


def _str_list(value: object) -> list[str]:
    """Coerce a value to a list of non-empty strings, discarding noise."""
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if x and str(x).strip()]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
