"""Layered configuration resolution.

Four layers compose, later overriding earlier:

1. built-in defaults — :class:`ConclaveConfig` instantiated with no args;
2. global overrides — the installation's preferences, the ``global-defaults`` config preset;
3. per-project overrides — the project's stored delta, on top of its integration branch (the
   ``default_branch`` column, which the project inherits as ``execution.target_branch``);
4. per-agent overrides — ``agent_overrides[name]`` deep-merged onto ``agent_defaults``
   (:func:`resolve_agent`, applied to the merged result of the first three).

Each layer is a sparse dict deep-merged onto the one below; a top-level ``workflow`` replaces
whole. A project stores only its delta — the keys whose value differs from what it inherits
from built-in + global + its branch — so later global and built-in changes keep reaching it,
and the effective ``execution.target_branch`` is the branch its tasks really merge into. Every
project config read and write goes through this module (:func:`resolve_project_config`,
:func:`save_project_config`, :func:`save_project_workflow`, :func:`create_project_with_config`,
:func:`save_global_config`), and a write that changes a project's workflow records a version.

This mirrors team-ai's ``models.overrides.{agent}`` → ``models.default`` semantics,
but fully typed and validated.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .graph import WorkflowGraph
from .models import (
    AgentSettings,
    BugFixerSessionConfig,
    BugFixerSessionOverride,
    ConclaveConfig,
)

if TYPE_CHECKING:
    # Only for annotations: the db layer ranks above config (hot reload, migrations), so the
    # DB-aware functions below import it inside their bodies.
    from ..db import Database, Project, WorkflowVersion, WorkflowVersionSource

# Safety floor: these are always protected regardless of project config. Users may
# ADD protected paths via config but can never remove these (closes a team-ai footgun).
PROTECTED_FLOOR_FILES = ("*.env", "*.env.*", ".env")
PROTECTED_FLOOR_DIRS = (".git",)

# Store name of the global layer (a ``config_preset`` item).
GLOBAL_DEFAULTS_PRESET = "global-defaults"

# Top-level keys merged, compared and unset whole rather than key by key.
_WHOLE_KEYS = frozenset({"workflow"})

# Reason recorded on a workflow version when the write's caller gave none.
_DEFAULT_WORKFLOW_REASON = "project settings saved"


def _unquote_key(k: str) -> str:
    cleaned = k.strip()
    while len(cleaned) >= 2:
        if (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()
        elif (cleaned.startswith('\\"') and cleaned.endswith('\\"') and len(cleaned) >= 4) or (
            cleaned.startswith("\\'") and cleaned.endswith("\\'") and len(cleaned) >= 4
        ):
            cleaned = cleaned[2:-2].strip()
        else:
            break
    return cleaned or k


def normalize_config_keys(value: Any) -> Any:
    """Recursively strip accidental surrounding quotes from dictionary keys (e.g.
    ``'"architect-as-planner"'`` -> ``'architect-as-planner'``)."""
    if isinstance(value, dict):
        return {
            (_unquote_key(k) if isinstance(k, str) else k): normalize_config_keys(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalize_config_keys(item) for item in value]
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Dict values are merged key-by-key; every other type (including lists) is replaced
    wholesale by the override value.
    """
    norm_base = normalize_config_keys(base)
    norm_override = normalize_config_keys(override)
    result: dict[str, Any] = dict(norm_base)
    for key, value in norm_override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _apply_layer(base: dict[str, Any], layer: dict[str, Any] | None) -> dict[str, Any]:
    """:func:`deep_merge` *layer* onto *base*, except that a top-level ``workflow`` replaces."""
    if not layer:
        return base
    norm_layer: dict[str, Any] = normalize_config_keys(layer)
    merged = deep_merge(base, {k: v for k, v in norm_layer.items() if k not in _WHOLE_KEYS})
    for key in _WHOLE_KEYS & norm_layer.keys():
        merged[key] = norm_layer[key]
    return merged


def _inherited_dict(
    global_overrides: dict[str, Any] | None, default_branch: str | None
) -> dict[str, Any]:
    """What a project inherits: built-in defaults -> global -> its integration branch.

    The branch (the project's ``default_branch`` column) is the ``execution.target_branch`` the
    project inherits, as the orchestrator merges into it unless the project's stored layer names
    another branch; a global ``target_branch`` therefore never moves an existing project.
    """
    # JSON mode, so stored (JSON) keys line up with the base: in python mode the int keys of
    # planning.level_thresholds would not merge with a stored "2", dropping its sibling fields.
    base = _apply_layer(ConclaveConfig().model_dump(mode="json"), global_overrides)
    if not default_branch:
        return base
    return _apply_layer(base, {"execution": {"target_branch": default_branch}})


def load_layered_config(
    global_overrides: dict[str, Any] | None = None,
    project_overrides: dict[str, Any] | None = None,
    *,
    default_branch: str | None = None,
) -> ConclaveConfig:
    """Build a validated :class:`ConclaveConfig`: built-in defaults -> global -> project.

    *default_branch* is the project's branch column, inherited as ``execution.target_branch``
    below its stored layer (see :func:`_inherited_dict`); ``None`` for the global view.
    """
    return ConclaveConfig.model_validate(
        _apply_layer(_inherited_dict(global_overrides, default_branch), project_overrides)
    )


def load_project_config(overrides: dict[str, Any] | None = None) -> ConclaveConfig:
    """Built-in defaults + *overrides*, without the global layer.

    Runtime code resolves a project through :func:`resolve_project_config`; this stays for
    validating standalone overrides (store presets) and for tests.
    """
    return load_layered_config(None, overrides)


def config_delta(effective: dict[str, Any], inherited: dict[str, Any]) -> dict[str, Any]:
    """The keys of *effective* whose value differs from *inherited* (both JSON-mode dumps).

    Dicts are compared key by key at every depth; a top-level ``workflow``, lists and scalars
    are compared whole; a key missing from *inherited* is kept whole. Layering only adds keys,
    so applying the result onto *inherited* gives back *effective*.
    """
    return _delta(effective, inherited, top=True)


def _delta(effective: dict[str, Any], inherited: dict[str, Any], *, top: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in effective.items():
        if key not in inherited:
            out[key] = value
            continue
        base = inherited[key]
        if isinstance(value, dict) and isinstance(base, dict) and not (top and key in _WHOLE_KEYS):
            sub = _delta(value, base, top=False)
            if sub:
                out[key] = sub
        elif value != base:
            out[key] = value
    return out


def patch_leaf_paths(patch: dict[str, Any]) -> list[str]:
    """Dotted paths of the values *patch* sets.

    Dicts recurse; a top-level ``workflow``, lists and scalars are leaves. An empty dict sets
    nothing, so it yields no path.
    """
    paths: list[str] = []

    def walk(node: dict[str, Any], prefix: str, top: bool) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict) and not (top and key in _WHOLE_KEYS):
                walk(value, f"{path}.", False)
            else:
                paths.append(path)

    walk(normalize_config_keys(patch), "", True)
    return paths


def validate_unset_paths(paths: Iterable[str]) -> list[str]:
    """Check dotted unset paths and return them with each key's stray quotes stripped.

    Raises ``ValueError`` unless every segment is non-empty, the first names a
    :class:`ConclaveConfig` field, and nothing sits below ``workflow`` (unset whole).
    """
    clean: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            raise ValueError(f"unset paths must be strings, got {path!r}")
        segments = [_unquote_key(segment) for segment in path.split(".")]
        if any(not segment.strip() for segment in segments):
            raise ValueError(f"invalid unset path {path!r}: every dotted segment must be non-empty")
        if segments[0] not in ConclaveConfig.model_fields:
            raise ValueError(f"invalid unset path {path!r}: unknown config section {segments[0]!r}")
        if segments[0] in _WHOLE_KEYS and len(segments) > 1:
            raise ValueError(f"invalid unset path {path!r}: unset {segments[0]!r} whole")
        clean.append(".".join(segments))
    return clean


def remove_config_paths(layer: dict[str, Any], paths: Iterable[str]) -> dict[str, Any]:
    """A copy of *layer* without the dotted *paths*, pruning parents the removal leaves empty."""
    result: dict[str, Any] = normalize_config_keys(layer)
    for path in paths:
        _remove_path(result, path.split("."))
    return result


def _remove_path(node: dict[str, Any], segments: list[str]) -> None:
    head, rest = segments[0], segments[1:]
    if head not in node:
        return
    if not rest:
        del node[head]
        return
    child = node[head]
    if isinstance(child, dict):
        _remove_path(child, rest)
        if not child:
            del node[head]


def apply_project_overrides(
    global_overrides: dict[str, Any] | None,
    stored: dict[str, Any] | None,
    patch: dict[str, Any] | None = None,
    unset: Iterable[str] = (),
    *,
    default_branch: str | None = None,
) -> tuple[ConclaveConfig, dict[str, Any]]:
    """Apply one write to a project's stored layer; return ``(effective config, minimal delta)``.

    The *unset* paths are removed from the stored layer first, then *patch* is merged onto the
    resolved document and the result is validated, so bad input raises ``ValueError``
    (including pydantic's ``ValidationError``) before anything is stored. The delta keeps only
    what differs from built-in + global + *default_branch* (the project's branch column, after
    this write): a full resolved document reduces to ``{}``, a key set back to the inherited
    value is dropped, and an ``execution.target_branch`` survives only when it names another
    branch than the column, so the branch the project merges into never changes.
    """
    inherited = _inherited_dict(global_overrides, default_branch)
    layer = remove_config_paths(stored or {}, validate_unset_paths(unset))
    effective = ConclaveConfig.model_validate(_apply_layer(_apply_layer(inherited, layer), patch))
    baseline = ConclaveConfig.model_validate(inherited).model_dump(mode="json")
    return effective, config_delta(effective.model_dump(mode="json"), baseline)


def apply_global_overrides(
    global_overrides: dict[str, Any] | None,
    patch: dict[str, Any] | None = None,
    unset: Iterable[str] = (),
) -> tuple[ConclaveConfig, dict[str, Any]]:
    """Apply one write to the global layer; return ``(resolved global config, new payload)``."""
    payload = _apply_layer(
        remove_config_paths(global_overrides or {}, validate_unset_paths(unset)), patch
    )
    return load_layered_config(payload), payload


def _stored_workflow(
    global_overrides: dict[str, Any] | None, stored: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The workflow a project gets from its stored layers (its own, else the global one), as
    a graph dump; ``None`` is the derived pipeline.

    Read from the workflow layers alone rather than by resolving the whole config, so a write
    that repairs an invalid stored layer still works; a workflow that does not validate is
    kept as stored.
    """
    for layer in (stored, global_overrides):
        norm_layer: dict[str, Any] = normalize_config_keys(layer or {})
        if "workflow" in norm_layer:
            raw = norm_layer["workflow"]
            break
    else:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return WorkflowGraph.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return raw


def _target_branch(layer: dict[str, Any] | None) -> str:
    execution = (layer or {}).get("execution")
    raw = execution.get("target_branch") if isinstance(execution, dict) else None
    return str(raw).strip() if raw else ""


def patched_branch(project: Project, patch: dict[str, Any] | None) -> str | None:
    """The branch a config write moves *project* to, or ``None`` when it does not move it.

    That is *patch*'s ``execution.target_branch`` when it names another branch than the one
    the project merges into now (its stored target, else its ``default_branch``, as the
    orchestrator resolves it). Restating the current branch, as a whole effective document
    sent back does, leaves the ``default_branch`` column alone.
    """
    branch = _target_branch(patch)
    current = _target_branch(project.config) or project.default_branch
    return branch if branch and branch != current else None


# --- DB-aware entry points --------------------------------------------------


async def load_global_overrides(db: Database) -> dict[str, Any]:
    """The global layer: the ``global-defaults`` config preset payload (``{}`` when unset)."""
    from ..db import StoreItemKind
    from ..db import repositories as repo

    item = await repo.get_store_item_by_name(
        db, StoreItemKind.config_preset, GLOBAL_DEFAULTS_PRESET
    )
    return dict(item.payload) if item is not None else {}


async def resolve_project_config(db: Database, project: Project | None) -> ConclaveConfig:
    """The effective config of *project* (built-in -> global -> project); global for ``None``."""
    global_overrides = await load_global_overrides(db)
    if project is None:
        return load_layered_config(global_overrides)
    return load_layered_config(
        global_overrides, project.config, default_branch=project.default_branch
    )


async def _write_project_config(
    db: Database,
    project: Project,
    patch: dict[str, Any] | None,
    *,
    unset: Iterable[str] = (),
    name: str | None = None,
    default_branch: str | None = None,
    workflow_source: WorkflowVersionSource | None = None,
    workflow_reason: str | None = None,
    approves_proposal: str | None = None,
    decision_reason: str | None = None,
) -> tuple[ConclaveConfig, WorkflowVersion | None]:
    """The one write of a project's stored layer, so no path can skip workflow versioning.

    Stores the minimal delta and, when the write changes the project's effective workflow, a
    version (the graph before and after it) in the same transaction; with
    *approves_proposal* that proposal is marked approved in it too.
    """
    from ..db import WorkflowVersionSource
    from ..db import repositories as repo

    global_overrides = await load_global_overrides(db)
    effective, delta = apply_project_overrides(
        global_overrides,
        project.config,
        patch,
        unset,
        default_branch=default_branch or project.default_branch,
    )
    before = _stored_workflow(global_overrides, project.config)
    after = None if effective.workflow is None else effective.workflow.model_dump(mode="json")
    if before == after and approves_proposal is None:
        await repo.update_project(
            db, project.id, name=name, default_branch=default_branch, config=delta
        )
        return effective, None
    version = await repo.update_project_with_workflow_version(
        db,
        project.id,
        config=delta,
        name=name,
        default_branch=default_branch,
        source=workflow_source or WorkflowVersionSource.operator,
        reason=workflow_reason or _DEFAULT_WORKFLOW_REASON,
        graph_before=before,
        graph_after=after,
        approves_proposal=approves_proposal,
        decision_reason=decision_reason,
    )
    return effective, version


async def save_project_config(
    db: Database,
    project: Project,
    patch: dict[str, Any] | None = None,
    *,
    unset: Iterable[str] = (),
    name: str | None = None,
    default_branch: str | None = None,
    workflow_source: WorkflowVersionSource | None = None,
    workflow_reason: str | None = None,
) -> ConclaveConfig:
    """Apply a write to *project*, store its minimal delta, and return the effective config.

    Bad input raises ``ValueError`` before anything is written (see
    :func:`apply_project_overrides`). *name* / *default_branch* update those columns too; the
    delta is taken against the branch the project has after the write. A write that changes
    the project's workflow records a version: *workflow_source* (default ``operator``) and
    *workflow_reason* describe it.
    """
    effective, _ = await _write_project_config(
        db,
        project,
        patch,
        unset=unset,
        name=name,
        default_branch=default_branch,
        workflow_source=workflow_source,
        workflow_reason=workflow_reason,
    )
    return effective


async def save_project_workflow(
    db: Database,
    project: Project,
    workflow: dict[str, Any] | None,
    *,
    source: WorkflowVersionSource,
    reason: str,
    approves_proposal: str | None = None,
    decision_reason: str | None = None,
) -> tuple[ConclaveConfig, WorkflowVersion | None]:
    """Replace *project*'s workflow (``None`` derives it from the agents policy, even over a
    global custom workflow).

    Returns ``(effective config, version)``; the version is ``None`` when the workflow did
    not change. With *approves_proposal*, that pending proposal is marked approved in the same
    transaction (:class:`~conclave.db.WorkflowConflictError` when it is no longer pending).
    """
    return await _write_project_config(
        db,
        project,
        {"workflow": workflow},
        workflow_source=source,
        workflow_reason=reason,
        approves_proposal=approves_proposal,
        decision_reason=decision_reason,
    )


async def create_project_with_config(
    db: Database,
    *,
    name: str,
    path: str,
    default_branch: str,
    patch: dict[str, Any] | None = None,
) -> Project:
    """Insert a project whose stored config is the minimal delta of *patch* (validated first)."""
    from ..db import repositories as repo

    _, delta = apply_project_overrides(
        await load_global_overrides(db), None, patch, default_branch=default_branch
    )
    return await repo.create_project(
        db, name=name, path=path, default_branch=default_branch, config=delta
    )


def already_attached_message(project: Project) -> str:
    """Why an attach of *project*'s path was refused (it changes nothing), and what to do."""
    return (
        f"{project.path} is already attached as project {project.id} ({project.name!r}); "
        "nothing was changed. To change that project's settings or name, use "
        f"conclave_patch_config (config, name) or PATCH /api/projects/{project.id}/config. "
        "To run another configuration on this repository, attach a separate clone of it."
    )


async def save_global_config(
    db: Database,
    patch: dict[str, Any] | None = None,
    *,
    unset: Iterable[str] = (),
    apply_to_all_projects: bool = False,
    workflow_reason: str | None = None,
) -> tuple[ConclaveConfig, list[Project]]:
    """Apply a write to the global layer; return ``(resolved global config, updated projects)``.

    Projects inherit the new global values on their next resolution without being touched,
    except the integration branch, which each project inherits from its ``default_branch``.
    With *apply_to_all_projects* every patched and unset key is also removed from each
    project's stored delta, so the new global value reaches every project; a patched
    ``execution.target_branch`` also becomes each project's ``default_branch``. Every new
    delta is validated before the first write, so a bad input changes nothing. A project
    whose own workflow this removes records a version (reason *workflow_reason*).
    """
    from ..db import StoreItemKind
    from ..db import repositories as repo

    norm_patch: dict[str, Any] = normalize_config_keys(patch or {})
    clean_unset = validate_unset_paths(unset)
    resolved, payload = apply_global_overrides(
        await load_global_overrides(db), norm_patch, clean_unset
    )
    new_branch = _target_branch(norm_patch)
    projects = await repo.list_projects(db) if apply_to_all_projects else []
    cleared = [*patch_leaf_paths(norm_patch), *clean_unset]
    for proj in projects:
        try:
            apply_project_overrides(
                payload,
                proj.config,
                unset=cleared,
                default_branch=new_branch or proj.default_branch,
            )
        except ValueError as exc:
            raise ValueError(f"project {proj.name} ({proj.id}): {exc}") from exc

    await repo.upsert_store_item(
        db,
        kind=StoreItemKind.config_preset,
        name=GLOBAL_DEFAULTS_PRESET,
        description="Global default settings applied across Conclave repositories.",
        tags=["global", "defaults"],
        payload=payload,
        built_in=False,
    )
    updated: list[Project] = []
    for proj in projects:
        await save_project_config(
            db,
            proj,
            unset=cleared,
            default_branch=new_branch or None,
            workflow_reason=workflow_reason or "global settings applied to all projects",
        )
        fresh = await repo.get_project(db, proj.id)
        if fresh is not None:
            updated.append(fresh)
    return resolved, updated


def resolve_agent(config: ConclaveConfig, agent: str) -> AgentSettings:
    """Resolve the effective :class:`AgentSettings` for ``agent``.

    ``agent_defaults`` provides the base; any ``agent_overrides[agent]`` keys win.
    """
    base = config.agent_defaults.model_dump(mode="python")
    clean_agent = _unquote_key(agent)
    norm_overrides = normalize_config_keys(config.agent_overrides)
    override = norm_overrides.get(clean_agent) or config.agent_overrides.get(agent, {})
    merged = deep_merge(base, dict(override)) if override else base
    return AgentSettings.model_validate(merged)


def resolve_bug_fixer_session(
    config: ConclaveConfig,
    override: BugFixerSessionOverride | None = None,
) -> BugFixerSessionConfig:
    """Resolve the effective limits for one Bug-Fixer session.

    Mirrors :func:`resolve_agent`'s layered shape: a per-session ``override`` wins,
    falling back to the project's ``bug_fixer`` policy default. ``wall_clock_budget_minutes``
    adds one final fallback — when neither the override nor the policy pins it, the session
    reuses ``execution.wall_clock_budget_minutes`` so it shares the per-task retry-loop cap
    rather than running unbounded.
    """
    policy = config.bug_fixer
    ov = override or BugFixerSessionOverride()
    budget = ov.wall_clock_budget_minutes
    if budget is None:
        budget = policy.wall_clock_budget_minutes
    if budget is None:
        budget = config.execution.wall_clock_budget_minutes
    return BugFixerSessionConfig(
        max_candidates=(
            ov.max_candidates if ov.max_candidates is not None else policy.max_candidates
        ),
        max_attempts=(ov.max_attempts if ov.max_attempts is not None else policy.max_attempts),
        wall_clock_budget_minutes=budget,
    )


def effective_protected(config: ConclaveConfig) -> tuple[list[str], list[str]]:
    """Return ``(file_globs, dir_globs)`` with the safety floor always included."""
    files = list(dict.fromkeys([*PROTECTED_FLOOR_FILES, *config.protected.files]))
    dirs = list(dict.fromkeys([*PROTECTED_FLOOR_DIRS, *config.protected.directories]))
    return files, dirs
