# ruff: noqa: E501
"""Facade of the ``campus`` domain (Epic 43, FR-217..FR-221). Other domains and Epic 52 import from here.

Owner functions consumed by Epic 52 (foundation 0.7): ``funnel`` (campus tile), ``quick_search_drives`` /
``quick_search_events`` (``QuickHit`` types ``campus_drive`` / ``recruitment_event``) and the go-live import
``import_institution`` (FR-263; ``InstitutionImportIn`` in ``app/schemas/campus.py``, CSV column names = field names).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.campus import FunnelOut, InstitutionImportIn
from app.services.campus import drives, institutions
from app.services.campus import funnel as _funnel


async def funnel(
    ctx: RequestContext, db: AsyncSession, drive_id: uuid.UUID | None, start: date | None, end: date | None
) -> FunnelOut:
    """Per-Institution milestone counts (registered ... joined, reneged), scoped by ``campus:read``."""
    return await _funnel.funnel(ctx, db, drive_id, start, end)


async def quick_search_drives(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Any]:
    return await drives.quick_search(ctx, db, q, limit)


async def quick_search_events(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Any]:
    from app.core.permissions import scope_condition
    from app.models.campus import RecruitmentEvent
    from app.schemas.search import QuickHit
    from app.services.campus.common import principal_of

    term = (q or "").strip()[:100]
    if not term:
        return []
    like = "%" + term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    stmt = select(RecruitmentEvent).where(RecruitmentEvent.name.ilike(like, escape="\\"))
    p = principal_of(ctx)
    if p is not None:
        cond = scope_condition(p, "event:manage" if p.has("event:manage") else "campus:read", RecruitmentEvent.owner_id)
        if cond is not None:
            stmt = stmt.where(cond)
    rows = (await db.execute(stmt.order_by(RecruitmentEvent.starts_at.desc()).limit(max(1, min(limit, 50))))).scalars()
    return [
        QuickHit(
            type="recruitment_event", id=e.id, title=e.name, subtitle=f"{e.starts_at:%d %b %Y} - {e.status}",
            url=f"/campus/events/{e.id}",
        )
        for e in rows
    ]


async def import_institution(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    data: InstitutionImportIn,
    *,
    validate_only: bool = False,
) -> uuid.UUID | None:
    return await institutions.import_institution(db, data, validate_only=validate_only)


__all__ = ["funnel", "import_institution", "quick_search_drives", "quick_search_events"]
