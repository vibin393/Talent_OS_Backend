"""Activity-domain erasure handler ``activity.entity`` (50) and retention signal (Epic 12, FR-68).

Notes, calls, meetings, emails and system entries on the candidate's timeline: ``body`` -> ``[erased]``,
``participants`` -> ``[]``, ``data`` -> NULL, and each entry's audit subject (``activity:<id>``, which encrypts
``body`` / ``participants`` in the audit log) is shredded. Logged emails (Epic 9) will clear subject, body and
recipients here too.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.activity import Activity
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_activity_signal, register_erasure_handler

ERASED = "[erased]"
ENTITY = "candidate"


def _stmt(ids: list[uuid.UUID]) -> Any:
    return select(Activity).where(Activity.entity_type == ENTITY, Activity.entity_id.in_(ids))


class ActivityEntityHandler:
    name = "activity.entity"
    order = 50
    tables = ("activities",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        rows = list((await db.execute(_stmt(ids))).scalars())
        for row in rows:
            row.body = ERASED
            row.participants = []
            row.data = None
        await db.flush()
        for row in rows:
            await audit.shred("activity", row.id, session=db)
        return {"activities": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (await db.execute(_stmt([ctx.candidate_id]).order_by(Activity.occurred_at))).scalars()
        return {
            "activities": [{"kind": r.kind, "occurred_at": r.occurred_at, "body": r.body} for r in rows if r.kind != "system"]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        return [f"activity {r.id} body remains" for r in (await db.execute(_stmt(ids))).scalars() if r.body != ERASED or r.participants]


async def activity_signal(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """Retention: the latest timeline entry about the candidate counts as activity."""
    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(Activity.entity_id, func.max(Activity.occurred_at))
        .where(Activity.entity_type == ENTITY, Activity.entity_id.in_(candidate_ids))
        .group_by(Activity.entity_id)
    )
    return dict(rows.tuples().all())


register_erasure_handler(ActivityEntityHandler())
register_activity_signal("activity.timeline", activity_signal)
