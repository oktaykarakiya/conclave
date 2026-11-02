"""Reasoning effort per dispatch: an unset effort means auto, an unsupported one is clamped.

``AgentSettings.effort`` of ``None`` is "auto", resolved for the engine that runs the model:

- Claude Code: ``xhigh``. Its CLI accepts low, medium, high, xhigh and max; ``max`` is never
  the automatic choice.
- opencode: the highest variant the model reports (``opencode models <provider> --verbose``),
  and no variant flag when none is known.

An explicit level the model does not support is clamped to the nearest supported level instead
of failing the dispatch. :func:`choose_effort` is the pure rule; :class:`OpencodeVariants` reads
and caches opencode's per-model variants. The router's ``resolve_dispatch_effort`` combines them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Effort
from . import opencode_cli
from .claude_cli import _WIN_SUBPROCESS_KW, _kill_process_group, _resolve_cli_exec

logger = logging.getLogger("conclave.providers.effort")

# Every level an engine may report, lowest first. The distance between two levels decides
# which supported level replaces an unsupported one.
EFFORT_SCALE: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_RANK = {level: rank for rank, level in enumerate(EFFORT_SCALE)}
# The levels auto may pick: the configurable ones, so never none or minimal.
_AUTO_LEVELS: tuple[str, ...] = tuple(e.value for e in Effort)

CLAUDE_CODE_EFFORTS: frozenset[str] = frozenset(_AUTO_LEVELS)
CLAUDE_CODE_AUTO_EFFORT: str = Effort.xhigh.value

# Upper bound on one ``opencode models`` run; past it the lookup counts as unknown.
_LOOKUP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class EffortChoice:
    """The effort a dispatch asked for (``None`` = auto) and the one it runs with."""

    requested: str | None
    used: str | None

    @property
    def clamped(self) -> bool:
        """An explicit level was replaced because the model does not support it."""
        return self.requested is not None and self.used != self.requested


def choose_effort(
    requested: str | None, supported: frozenset[str] | None, *, auto: str | None = None
) -> EffortChoice:
    """Pick the effort for one dispatch (pure).

    *supported* is the set of levels the model accepts, or ``None`` when that is unknown. Auto
    (*requested* ``None``) uses *auto* when given, else the highest supported configurable
    level, else no level. An explicit level is kept when support is unknown, when it is
    supported, or when it is not on :data:`EFFORT_SCALE` (a custom variant). Otherwise it is
    clamped to the nearest supported level on the scale (the lower one on a tie), or to no level
    when the model supports none.
    """
    if requested is None:
        if auto is not None:
            return EffortChoice(None, auto)
        levels = [level for level in _AUTO_LEVELS if supported and level in supported]
        return EffortChoice(None, levels[-1] if levels else None)
    if supported is None or requested in supported or requested not in _RANK:
        return EffortChoice(requested, requested)
    candidates = [level for level in EFFORT_SCALE if level in supported]
    if not candidates:
        return EffortChoice(requested, None)
    want = _RANK[requested]
    used = min(candidates, key=lambda level: (abs(_RANK[level] - want), _RANK[level]))
    return EffortChoice(requested, used)


class VariantLookup(Protocol):
    """Reports the reasoning variants an opencode ``provider/model`` supports."""

    async def lookup(self, model: str) -> frozenset[str] | None:
        """The model's variants, or ``None`` when unknown. Must not raise."""
        ...


def parse_verbose_models(text: str) -> dict[str, frozenset[str]]:
    """Map each model in ``opencode models <provider> --verbose`` output to its variants.

    The output is a ``provider/model`` line followed by that model's JSON object, per model. A
    model whose JSON has no ``variants`` object is left out (unknown); ``{}`` gives an empty set.
    Raises ``ValueError`` on malformed JSON.
    """
    decoder = json.JSONDecoder()
    table: dict[str, frozenset[str]] = {}
    header = ""
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text[pos] == "{":
            obj, pos = decoder.raw_decode(text, pos)
            variants = obj.get("variants") if isinstance(obj, dict) else None
            if header and isinstance(variants, dict):
                table[header] = frozenset(str(name) for name in variants)
            header = ""
        else:
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            header = text[pos:end].strip()
            pos = end
    return table


class OpencodeVariants:
    """The variants opencode reports per model, read once per provider and kept in memory.

    The first lookup for a provider runs ``opencode models <provider> --verbose`` under a
    timeout and caches that provider's table for the daemon's lifetime. A missing binary, a
    timeout, a non-zero exit or unreadable output is cached as unknown, so no dispatch waits on
    it twice; a restart reads it again. :meth:`lookup` never raises (cancellation aside).
    """

    def __init__(self, *, timeout_seconds: float = _LOOKUP_TIMEOUT_S) -> None:
        self._timeout = timeout_seconds
        self._tables: dict[str, dict[str, frozenset[str]] | None] = {}
        self._lock = asyncio.Lock()

    async def lookup(self, model: str) -> frozenset[str] | None:
        provider_id, sep, _name = model.partition("/")
        provider_id = provider_id.strip()
        if not sep or not provider_id:
            return None
        async with self._lock:
            if provider_id not in self._tables:
                try:
                    table = await self._read_provider(provider_id)
                except Exception:
                    logger.warning("opencode variants for %s unknown", provider_id, exc_info=True)
                    table = None
                self._tables[provider_id] = table
        found = self._tables[provider_id]
        return found.get(model.strip()) if found is not None else None

    async def _read_provider(self, provider_id: str) -> dict[str, frozenset[str]] | None:
        # The binary is read at call time, so an override (or a hot reload) applies at once.
        cli = opencode_cli._OPENCODE_BIN
        exec_bin, exec_args = _resolve_cli_exec(cli, ["models", provider_id, "--verbose"])
        try:
            proc = await asyncio.create_subprocess_exec(
                exec_bin,
                *exec_args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                **_WIN_SUBPROCESS_KW,
            )
        except OSError as exc:
            logger.info("opencode variants for %s unknown: %s", provider_id, exc)
            return None
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), self._timeout)
        except TimeoutError:
            logger.warning(
                "opencode variants for %s unknown: timed out after %ss", provider_id, self._timeout
            )
            return None
        finally:
            if proc.returncode is None:  # timed out or cancelled: leave no process behind
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
        if proc.returncode != 0:
            logger.info("opencode variants for %s unknown: exit %s", provider_id, proc.returncode)
            return None
        return parse_verbose_models(out.decode("utf-8", errors="replace"))
