"""Seed global default agent personas and Global Store blueprints on startup.

Idempotent and non-destructive — only inserts what is missing, so operator or MCP
edits to personas, workflows, and config presets in the UI/Store are preserved across
restarts and reusable across any repository.
"""

from __future__ import annotations

from typing import Any

from .agents import DEFAULT_PERSONAS
from .config import workflow_templates
from .db import Database, StoreItemKind
from .db import repositories as repo

_BUILT_IN_CONFIG_PRESETS: dict[str, tuple[str, list[str], dict[str, Any]]] = {
    "default-balanced": (
        "Balanced single-flight execution with auto-merge and full green-gate enforcement.",
        ["default", "balanced", "green-gate"],
        {
            "execution": {
                "max_parallel_tasks": 1,
                "parallel_reviewers": False,
                "auto_merge": True,
                "require_full_green": True,
            }
        },
    ),
    "high-throughput-swarm": (
        "Parallel worktree swarm execution (4 concurrent sessions + parallel reviewers).",
        ["swarm", "parallel", "mass-refactor"],
        {
            "execution": {
                "max_parallel_tasks": 4,
                "parallel_reviewers": True,
                "auto_merge": True,
                "require_full_green": True,
            }
        },
    ),
    "strict-tdd-audit": (
        "Strict TDD & grounded security review with parallel reviewer swarm.",
        ["tdd", "security", "audit"],
        {
            "execution": {
                "max_parallel_tasks": 2,
                "parallel_reviewers": True,
                "auto_merge": True,
                "require_full_green": True,
            },
            "experimental": {
                "grounding_checks": True,
                "planner_enabled": True,
                "cross_attempt_memory": True,
            },
        },
    ),
    "gemini-flash-lite-swarm": (
        (
            "Ultra-low-cost parallel swarm powered by google/gemini-2.5-flash-lite "
            "with pre-dispatch compaction."
        ),
        ["gemini", "flash-lite", "swarm", "low-cost"],
        {
            "agent_defaults": {
                "model": "google/gemini-2.5-flash-lite",
                "compact_before_start": True,
            },
            "execution": {
                "max_parallel_tasks": 3,
                "parallel_reviewers": True,
                "auto_merge": True,
                "require_full_green": True,
            },
        },
    ),
}


async def seed_global_defaults(db: Database) -> None:
    # 1. Seed global agent personas + mirror into Global Store
    for name, (role, persona_md) in DEFAULT_PERSONAS.items():
        if await repo.get_agent(db, name) is None:
            await repo.upsert_agent(db, name=name, role=role.value, persona_md=persona_md)
        if await repo.get_store_item_by_name(db, StoreItemKind.agent, name) is None:
            first_line = next(
                (
                    line.lstrip("# ").strip()
                    for line in persona_md.splitlines()
                    if line.strip()
                ),
                name,
            )
            await repo.upsert_store_item(
                db,
                kind=StoreItemKind.agent,
                name=name,
                description=f"{first_line} ({role.value} role)",
                tags=[role.value, "built-in"],
                payload={"name": name, "role": role.value, "persona_md": persona_md},
                built_in=True,
            )

    # 2. Seed built-in workflow diagram blueprints into Global Store
    for tpl_id, graph in workflow_templates().items():
        if await repo.get_store_item_by_name(db, StoreItemKind.workflow, tpl_id) is None:
            await repo.upsert_store_item(
                db,
                kind=StoreItemKind.workflow,
                name=tpl_id,
                description=graph.description,
                tags=["workflow", tpl_id, "built-in"],
                payload=graph.model_dump(mode="json"),
                built_in=True,
            )

    # 3. Seed built-in config presets into Global Store
    for preset_name, (desc, tags, cfg_payload) in _BUILT_IN_CONFIG_PRESETS.items():
        if (
            await repo.get_store_item_by_name(db, StoreItemKind.config_preset, preset_name)
            is None
        ):
            await repo.upsert_store_item(
                db,
                kind=StoreItemKind.config_preset,
                name=preset_name,
                description=desc,
                tags=tags,
                payload=cfg_payload,
                built_in=True,
            )
