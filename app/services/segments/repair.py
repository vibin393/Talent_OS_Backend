# ruff: file-ignore[line-too-long]
"""Segment repair (spec A3): definitions whose fields / joins / sources disappeared from the semantic registry.

``check_all`` runs nightly (the ``talent_pools.tick`` beat) and before every refresh (``issues_of``): a
``semantic_unknown_*`` error flags ``needs_repair`` with ``repair_issues`` (``details.path``); a definition that
validates again is cleared. Pools of a flagged Segment skip their refresh (``skipped_needs_repair``).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.semantic import SemanticError, validate
from app.models.talent_pools import Segment

__all__ = ["REPAIR_CODES", "check_all", "flag", "issue", "issues_of"]

REPAIR_CODES = frozenset(
    {
        "semantic_unknown_source",
        "semantic_unknown_field",
        "semantic_unknown_join",
        "semantic_operator_not_allowed",
    }
)


def issue(exc: SemanticError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, "path": exc.path}


def issues_of(segment: Segment) -> list[dict[str, Any]]:
    """Registry problems of the stored definition (no principal: author permissions were checked at save)."""
    try:
        validate(segment.definition)
    except SemanticError as exc:
        if exc.code in REPAIR_CODES:
            return [issue(exc)]
        return []
    return []


def flag(segment: Segment, issues: list[dict[str, Any]]) -> None:
    segment.needs_repair = True
    segment.repair_issues = issues


async def check_all(db: AsyncSession) -> list[Segment]:
    """Flag / clear every active segment of the session's tenant; returns the newly flagged ones."""
    newly: list[Segment] = []
    for segment in (await db.execute(select(Segment).where(Segment.archived_at.is_(None)))).scalars():
        issues = issues_of(segment)
        if issues:
            if not segment.needs_repair:
                newly.append(segment)
            flag(segment, issues)
        elif segment.needs_repair:
            segment.needs_repair = False
            segment.repair_issues = []
    await db.flush()
    return newly
