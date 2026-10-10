"""Thin wrapper over the activity service (Epic 2) used by the candidates service.

Keeps every timeline call of the candidates package in one place: system entries (never PII in the body
or data), the candidate entity hook for timeline / notes, and activity moves on merge / undo.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services import activity_service
from app.services.activity_service import EntityChecker


async def add_system_entry(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID, body: str, **data: Any
) -> None:
    """Timeline system entry (ids, counts and field names only -- never PII)."""
    await activity_service.add_system_entry(
        ctx, db, entity_type, entity_id, body, {k: v for k, v in data.items() if v is not None} or None
    )


async def reassign_entity(
    ctx: RequestContext, db: AsyncSession, entity_type: str, from_id: uuid.UUID, to_id: uuid.UUID
) -> list[uuid.UUID]:
    """Move all activities of one record to another (merge). Returns the moved activity ids."""
    return await activity_service.reassign_entity(ctx, db, entity_type, from_id, to_id)


async def reassign_activities(
    ctx: RequestContext, db: AsyncSession, entity_type: str, activity_ids: list[uuid.UUID], to_id: uuid.UUID
) -> None:
    """Undo of a merge: move exactly these activities back."""
    await activity_service.reassign_activities(ctx, db, entity_type, activity_ids, to_id)


def register_entity(entity_type: str, checker: EntityChecker, *, plural: str | None = None) -> None:
    """Enable the Epic 2 timeline / notes endpoints for candidates (``/api/v1/candidates/{id}/timeline``)."""
    activity_service.register_entity(entity_type, checker, plural=plural)
