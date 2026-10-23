# ruff: file-ignore[line-too-long]
"""Match list section registry (Epic 41, FR-212; AD-40 registry pattern).

Other domains register *sections* rendered above the P1 match list: Epic 41 registers ``internal`` (order 20; a
bench section, when present, keeps order 10). ``matching`` never imports those domains -- they call
``register_section`` from their ``hooks.py``.

A section's ``candidate_ids`` callable is ``async (ctx, db, demand_id) -> set[UUID] | None``; ``None`` hides the
section for this demand (e.g. a Demand with ``visibility='external'`` has no internal section). The section items
are the **stored** ``matches`` rows of those candidates (``service.scores_for_candidates``): scores are never
recomputed here (AD-8). A failing loader is logged and omitted; it never breaks the match list. The default
ordering and the existing list fields are unchanged.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.schemas.matching import MatchSectionOut

__all__ = ["CandidateIdsLoader", "SectionDef", "build_sections", "register_section", "sections", "unregister_section"]

CandidateIdsLoader = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[set[uuid.UUID] | None]]


@dataclasses.dataclass(frozen=True, slots=True)
class SectionDef:
    key: str
    order: int
    label: str
    candidate_ids: CandidateIdsLoader


_sections: dict[str, SectionDef] = {}


def register_section(key: str, *, order: int, label: str, candidate_ids: CandidateIdsLoader) -> None:
    """Add (or replace) a section; keys are short domain names (``internal``, ``bench``)."""
    if not key or not key.replace("_", "").isalnum():
        msg = f"bad match section key {key!r}"
        raise ValueError(msg)
    _sections[key] = SectionDef(key, order, label, candidate_ids)


def unregister_section(key: str) -> None:
    _sections.pop(key, None)


def sections() -> list[SectionDef]:
    return sorted(_sections.values(), key=lambda s: (s.order, s.key))


async def build_sections(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[MatchSectionOut]:
    from app.services.matching import service

    out: list[MatchSectionOut] = []
    for definition in sections():
        try:
            async with db.begin_nested():
                ids = await definition.candidate_ids(ctx, db, demand_id)
        except DomainError as exc:
            logger.info("match section {} skipped: {}", definition.key, exc.code)
            continue
        except Exception:
            logger.exception("match section {} failed", definition.key)
            continue
        if ids is None:
            continue
        items = await service.scores_for_candidates(ctx, db, demand_id, sorted(ids, key=str))
        items = [m for m in items if m.dismissed_at is None]
        out.append(
            MatchSectionOut(key=definition.key, label=definition.label, order=definition.order, total=len(items), items=items)
        )
    return out
