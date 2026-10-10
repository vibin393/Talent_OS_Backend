"""Story 22.9 (A10) erasure handler ``notifications.texts`` (57; FR-68 / AD-34).

* Notifications addressed to the subject (portal inbox: ``recipient_kind`` = the subject kind and
  ``recipient_id`` = the subject) are deleted together with their ``notification_deliveries``.
* Staff notifications about a candidate (``entity_type='candidate'``): title -> ``[erased]``, body and link
  cleared (the entity reference stays -- ids only).

Rows in ``ctx.retained["notifications"]`` are kept.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notifications import Notification, NotificationDelivery
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _ids(db: AsyncSession, ctx: ErasureContext) -> list[uuid.UUID]:
    if ctx.subject_kind == "candidate":
        return await subject_ids(db, ctx.candidate_id)
    return [ctx.subject_id] if ctx.subject_id is not None else []


def _addressed(kind: str, ids: list[uuid.UUID]) -> Any:
    return select(Notification).where(Notification.recipient_kind == kind, Notification.recipient_id.in_(ids))


def _about(ids: list[uuid.UUID]) -> Any:
    return select(Notification).where(
        Notification.recipient_kind != "candidate",
        Notification.entity_type == "candidate",
        Notification.entity_id.in_(ids),
    )


class NotificationTextsHandler:
    name = "notifications.texts"
    order = 57
    tables = ("notifications", "notification_deliveries")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await _ids(db, ctx)
        keep = ctx.retained.get("notifications", set())
        doomed = [
            n.id for n in (await db.execute(_addressed(ctx.subject_kind, ids))).scalars() if n.id not in keep
        ]
        deliveries = 0
        if doomed:
            deleted = await db.execute(
                delete(NotificationDelivery)
                .where(NotificationDelivery.notification_id.in_(doomed))
                .returning(NotificationDelivery.id)
            )
            deliveries = len(deleted.all())
            await db.execute(delete(Notification).where(Notification.id.in_(doomed)))
        about: list[Notification] = []
        if ctx.subject_kind == "candidate":
            about = [n for n in (await db.execute(_about(ids))).scalars() if n.id not in keep]
            for n in about:
                n.title, n.body, n.link_path = ERASED, None, None
        await db.flush()
        return {"notifications": len(doomed) + len(about), "notification_deliveries": deliveries}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (await db.execute(_addressed(ctx.subject_kind, await _ids(db, ctx)))).scalars()
        return {
            "notifications": [
                {"type": n.type, "title": n.title, "body": n.body, "at": n.created_at} for n in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await _ids(db, ctx)
        keep = ctx.retained.get("notifications", set())
        issues = [
            f"notification {n.id} addressed to the subject remains"
            for n in (await db.execute(_addressed(ctx.subject_kind, ids))).scalars()
            if n.id not in keep
        ]
        if ctx.subject_kind == "candidate":
            issues += [
                f"notification {n.id} keeps a title or body about the candidate"
                for n in (await db.execute(_about(ids))).scalars()
                if n.id not in keep and (n.title != ERASED or n.body is not None)
            ]
        return issues


register_erasure_handler(NotificationTextsHandler())
