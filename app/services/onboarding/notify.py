# ruff: noqa: E501
"""Onboarding notifications (Epic 23 A8). Delivered in the caller's transaction; a delivery failure never rolls
back the business change (savepoint)."""

import uuid
from collections.abc import Iterable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

INVITED = "onboarding_invited"
TASK_RETURNED = "onboarding_task_returned"
TASK_OVERDUE = "onboarding_task_overdue"
REQUIREMENTS_ADDED = "onboarding.requirements_added"
NO_TEMPLATE = "onboarding.no_template"
PORTAL_LINK = "/portal/work/onboarding"


async def to_worker(
    db: AsyncSession, type_key: str, candidate_id: uuid.UUID, title: str, body: str | None = None
) -> None:
    from app.services.notifications_service import Content, Recipient, deliver

    try:
        async with db.begin_nested():
            await deliver(
                db,
                type_key,
                [Recipient("candidate", candidate_id)],
                Content(title=title[:300], body=body, link_path=PORTAL_LINK),
                event_id=str(uuid.uuid4()),
            )
    except Exception:
        logger.exception("onboarding notification {} to the worker failed", type_key)


async def to_users(
    db: AsyncSession,
    type_key: str,
    user_ids: Iterable[uuid.UUID | None],
    title: str,
    *,
    case_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
) -> None:
    from app.services.notifications_service import Content, deliver

    ids = [u for u in user_ids if u is not None]
    if not ids:
        return
    link = f"/onboarding/{case_id}" if case_id else (f"/placements/{placement_id}" if placement_id else None)
    try:
        async with db.begin_nested():
            await deliver(
                db,
                type_key,
                ids,
                Content(
                    title=title[:300],
                    entity_type="placement" if placement_id else None,
                    entity_id=placement_id,
                    link_path=link,
                ),
                event_id=str(uuid.uuid4()),
            )
    except Exception:
        logger.exception("onboarding notification {} failed", type_key)
