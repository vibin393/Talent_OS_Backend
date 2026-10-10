# ruff: noqa: E501
"""Erasure handler ``demand_intake.messages`` (order 68), Epic 55 (A8, FR-326).

Intake messages hold Client-contact data (sender address / name, free text) -- never candidate data. For a
**contact** subject, every message matched to that contact or sent from its address loses its content: the raw /
text / attachment files are purged, ``extraction`` / ``body_preview`` nulled, the sender address and name and the
subject cleared; the status and the draft links stay. Candidate (and other) subjects are a no-op. Raw content of every
message is also purged after 180 days (``demand_intake.purge_expired``).
"""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.demand_intake import DemandIntakeMessage
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class DemandIntakeMessagesHandler:
    name = "demand_intake.messages"
    order = 68
    tables = ("demand_intake_messages",)

    async def _rows(self, db: AsyncSession, ctx: ErasureContext) -> list[DemandIntakeMessage]:
        from app.models.clients import Contact

        contact = await db.get(Contact, ctx.subject_id)
        conds = [DemandIntakeMessage.matched_contact_id == ctx.subject_id]
        if contact is not None and contact.email:
            conds.append(DemandIntakeMessage.from_address == str(contact.email).lower())
        keep = ctx.retained.get("demand_intake_messages", set())
        rows = (await db.execute(select(DemandIntakeMessage).where(or_(*conds)))).scalars()
        return [r for r in rows if r.id not in keep]

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "contact":
            return {"demand_intake_messages": 0}
        from app.services.demand_intake.items import purge_content

        rows = await self._rows(db, ctx)
        for row in rows:
            await purge_content(db, row)
            row.from_address = None
            row.from_name = None
            row.subject = None
            row.matched_contact_id = None
        await db.flush()
        return {"demand_intake_messages": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "contact":
            return None
        rows = await self._rows(db, ctx)
        return {
            "demand_intake_messages": [
                {
                    "id": str(r.id),
                    "received_at": r.received_at.isoformat(),
                    "subject": r.subject,
                    "from": r.from_address,
                    "status": r.status,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "contact":
            return []
        left = (
            await db.execute(
                select(DemandIntakeMessage.id)
                .where(DemandIntakeMessage.matched_contact_id == ctx.subject_id)
                .limit(1)
            )
        ).first()
        return ["demand_intake_messages still linked to the contact"] if left else []


register_erasure_handler(DemandIntakeMessagesHandler())
