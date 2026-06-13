"""Repo-knowledge model, manifest fingerprinting, and prompt rendering."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# Files whose contents define the project's toolchain/deps; a change here means the
# learned knowledge may be stale and should be refreshed.
MANIFEST_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "uv.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Gemfile",
    "Dockerfile",
    "docker-compose.yml",
    "tsconfig.json",
)


class RepoKnowledge(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)
    architecture_summary: str = ""
    conventions: list[str] = Field(default_factory=list)
    layout: dict[str, list[str]] = Field(default_factory=dict)
    protected_globs: list[str] = Field(default_factory=list)


def manifest_fingerprint(repo_path: Path) -> str:
    """Hash of all present manifest files' contents — cheap staleness signal."""
    digest = hashlib.sha256()
    for name in sorted(MANIFEST_FILES):
        candidate = repo_path / name
        if candidate.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def render_preamble(knowledge: dict[str, Any]) -> str:
    """Render learned knowledge into a compact preamble prepended to every dispatch."""
    if not knowledge:
        return ""
    parts: list[str] = []
    summary = knowledge.get("architecture_summary")
    if isinstance(summary, str) and summary:
        parts.append(summary)
    langs = knowledge.get("languages")
    if isinstance(langs, list) and langs:
        parts.append("Languages: " + ", ".join(str(x) for x in langs))
    frameworks = knowledge.get("frameworks")
    if isinstance(frameworks, list) and frameworks:
        parts.append("Frameworks: " + ", ".join(str(x) for x in frameworks))
    commands = knowledge.get("commands")
    if isinstance(commands, dict) and commands:
        rendered = ", ".join(f"{k}: `{v}`" for k, v in commands.items() if v)
        if rendered:
            parts.append("Commands — " + rendered)
    conventions = knowledge.get("conventions")
    if isinstance(conventions, list) and conventions:
        parts.append("Conventions:\n" + "\n".join(f"- {c}" for c in conventions))
    return "\n".join(parts)
