"""Repo onboarding: learn languages, frameworks, and commands from manifests.

This MVP analysis is heuristic and deterministic (no LLM) so it is fast and testable;
an LLM ``repo-analyst`` enrichment pass can layer on later. Staleness is detected by a
manifest fingerprint so refreshes happen only when the toolchain actually changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db import Database, Project, RepoKnowledgeRow
from ..db import repositories as repo
from ..engine.gitio import run_git
from ..events import EventBus, EventType
from .knowledge import RepoKnowledge, manifest_fingerprint

_JS_FRAMEWORKS = (
    "react", "vue", "svelte", "next", "nuxt", "express", "fastify", "koa",
    "jest", "vitest", "mocha", "playwright", "@playwright/test",
)


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
    db: Database, bus: EventBus, project: Project, *, force: bool = False
) -> RepoKnowledgeRow:
    """Analyze the repo and persist knowledge, skipping if manifests are unchanged."""
    repo_path = Path(project.path)
    fingerprint = manifest_fingerprint(repo_path)
    code, sha_out = await run_git(repo_path, "rev-parse", "HEAD")
    sha = sha_out.strip() if code == 0 else None

    current = await repo.current_repo_knowledge(db, project.id)
    if current is not None and not force and current.manifest_fingerprint == fingerprint:
        return current

    await bus.emit(
        type=EventType.onboarding_started, project_id=project.id, payload={"force": force}
    )
    knowledge = analyze_repo(repo_path)
    row = await repo.save_repo_knowledge(
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
    return row


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
