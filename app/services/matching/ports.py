"""Pipeline port of the matching service (Epic 5).

Epic 6 (pipeline) implements ``PipelineGateway`` and registers it with
``app.services.matching.register_pipeline_gateway``; until then ``UnavailablePipelineGateway`` makes
add-to-pipeline answer 409 ``pipeline_unavailable`` and excludes nobody from the matching pool.
"""

# ruff: noqa: ARG002  (the default gateway ignores its arguments by design)

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext


class PipelineGateway(Protocol):
    def is_available(self) -> bool: ...

    async def add_candidate(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        demand_id: uuid.UUID,
        candidate_id: uuid.UUID,
        *,
        source: str = "ai_match",
        match_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...

    async def candidate_ids_for_demand(
        self, ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
    ) -> set[uuid.UUID]: ...


class UnavailablePipelineGateway:
    def is_available(self) -> bool:
        return False

    async def add_candidate(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        demand_id: uuid.UUID,
        candidate_id: uuid.UUID,
        *,
        source: str = "ai_match",
        match_id: uuid.UUID | None = None,
    ) -> uuid.UUID:  # pragma: no cover - guarded by is_available()
        msg = "the pipeline is not available yet"
        raise RuntimeError(msg)

    async def candidate_ids_for_demand(
        self, ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
    ) -> set[uuid.UUID]:
        return set()


_gateway: PipelineGateway = UnavailablePipelineGateway()


def register_pipeline_gateway(gateway: PipelineGateway | None) -> None:
    """Epic 6 registers its implementation; ``None`` restores the unavailable default (tests)."""
    global _gateway  # process-wide wiring
    _gateway = gateway or UnavailablePipelineGateway()


def pipeline_gateway() -> PipelineGateway:
    return _gateway


# ---------------------------------------------------------------- score suppression (Epic 49, additive)
# A suppressor receives ``(candidate_id, demand_id)`` pairs and returns the pairs whose AEDT outputs (AI Match
# overall / sub-scores / explanation, assessment auto-scores) must be hidden from recruiters -- as a mapping
# ``pair -> reason`` (``"alternative_process"`` | ``"notice_period"``) or a plain set (reason ``"hidden"``).
# Epic 49 (``fairness``) registers one from its hooks; P1 never imports it (registry, AD-40).
ScorePair = tuple[uuid.UUID, uuid.UUID]
ScoreSuppressor = Callable[
    [AsyncSession, Sequence[ScorePair]], Awaitable[Mapping[ScorePair, str] | AbstractSet[ScorePair]]
]
_score_suppressors: dict[str, ScoreSuppressor] = {}


def register_score_suppressor(fn: ScoreSuppressor, *, key: str | None = None) -> None:
    """Register (or replace, by ``key`` / function name) a score suppressor."""
    _score_suppressors[key or getattr(fn, "__qualname__", repr(fn))] = fn


def unregister_score_suppressor(key: str) -> None:
    _score_suppressors.pop(key, None)


async def suppressed_pairs(db: AsyncSession, pairs: Iterable[ScorePair]) -> dict[ScorePair, str]:
    """Return ``pair -> reason`` for every pair a registered suppressor hides (empty without suppressors).

    A failing suppressor is logged and skipped (it runs in a savepoint so the caller's transaction stays
    usable)."""
    wanted = list(dict.fromkeys(pairs))
    if not wanted or not _score_suppressors:
        return {}
    out: dict[ScorePair, str] = {}
    for key, fn in list(_score_suppressors.items()):
        try:
            async with db.begin_nested():
                hidden = await fn(db, wanted)
        except Exception:
            logger.exception("score suppressor {} failed", key)
            continue
        if isinstance(hidden, Mapping):
            for pair, reason in hidden.items():
                out.setdefault(pair, str(reason))
        else:
            for pair in hidden:
                out.setdefault(pair, "hidden")
    return out
