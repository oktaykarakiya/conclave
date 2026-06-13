"""Derive the reviewer pipeline from a diff (ported from team-ai).

Mandatory agents always run; conditional agents are added when the diff trips their
triggers — either *structural* (new files, file count) or *keyword* (path/content
substrings). Driven by the project's :class:`AgentsPolicy` so it is config-tunable.
"""

from __future__ import annotations

from ..config import AgentsPolicy

# Keyword triggers matched (case-insensitively) against the whole diff text.
_TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "dockerfile": ["Dockerfile", "docker-compose"],
    "env_var": [".env"],
    "db_schema": ["migration", "schema.sql", "CREATE TABLE", "ALTER TABLE", "schema"],
    "db_change": ["migration", "schema.sql", "CREATE TABLE", "ALTER TABLE", "schema"],
    "migration": ["migration"],
    "api_change": ["api", "route", "endpoint", "controller"],
    "new_dependency": ["package.json", "requirements.txt", "Cargo.toml", "pom.xml", "go.mod"],
    "frontend_bundle": ["package.json", "webpack", "vite", "rollup"],
    "auth_change": ["auth", "login", "password", "token", "jwt", "passport"],
    "payment_change": ["stripe", "payment", "checkout", "billing", "invoice"],
    "user_data_change": ["user", "profile", "gdpr", "privacy"],
    "third_party_api": ["fetch", "axios", "http", "stripe", "twilio", "mailgun"],
    "deploy_config": ["deploy", "ci", "cd", "pipeline", ".yml", ".yaml"],
    "new_service": ["service", "microservice", "worker"],
    "loop_operation": ["for ", "while ", "forEach", ".map(", ".reduce("],
    "infra_change": ["terraform", "ansible", "kubernetes", "k8s", "helm"],
}


def get_agent_pipeline(diff: str, policy: AgentsPolicy) -> list[str]:
    pipeline = list(policy.mandatory)

    diff_lower = diff.lower()
    lines = diff.split("\n")
    file_count = sum(1 for line in lines if line.startswith("diff --git"))
    has_new_files = any("new file mode" in line for line in lines)

    structural: dict[str, bool] = {
        "new_files": has_new_files,
        "files_gt_5": file_count > 5,
        "complexity_medium_plus": file_count > 3,
    }

    for agent, conditional in policy.conditional.items():
        if agent in pipeline:
            continue
        if _triggers_match(conditional.triggers, structural, diff_lower):
            pipeline.append(agent)

    return pipeline


def _triggers_match(
    triggers: list[str], structural: dict[str, bool], diff_lower: str
) -> bool:
    for trigger in triggers:
        if trigger in structural:
            if structural[trigger]:
                return True
            continue
        for keyword in _TRIGGER_KEYWORDS.get(trigger, []):
            if keyword.lower() in diff_lower:
                return True
    return False
