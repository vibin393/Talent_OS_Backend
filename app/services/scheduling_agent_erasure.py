# ruff: file-ignore[line-too-long]
"""Erasure handler ``scheduling_agent.requests`` (Epic 59, order 68; FR-326).

For a candidate subject (+ merged ids): null the Candidate's free text on offers (``candidate_note``,
``counter_windows``), the request's ``reschedule_reason`` and the external interviewers' names; delete the Candidate's
reminder rows. Request / offer headers and the history (ids and times only) stay for the audit trail.
A top-level module (``load_erasure_handlers``), so privacy never imports the Scheduling Agent.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduling_agent import SchedulingReminder, SchedulingRequest, SchedulingSlotOffer
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class SchedulingRequestsHandler:
    name = "scheduling_agent.requests"
    order = 68
    tables = (
        "scheduling_requests",
        "scheduling_slot_offers",
        "scheduling_request_events",
        "scheduling_reminders",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"scheduling_slot_offers": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        offers = await db.execute(
            update(SchedulingSlotOffer)
            .where(SchedulingSlotOffer.candidate_id.in_(ids))
            .values(candidate_note=None, counter_windows=None)
        )
        requests = list(
            (
                await db.execute(select(SchedulingRequest).where(SchedulingRequest.candidate_id.in_(ids)))
            ).scalars()
        )
        for req in requests:
            req.reschedule_reason = None
            req.external_participants = [
                {"email": e.get("email")} for e in req.external_participants or [] if isinstance(e, dict)
            ]
        reminders = await db.execute(
            delete(SchedulingReminder).where(SchedulingReminder.candidate_id.in_(ids))
        )
        await db.flush()
        return {
            "scheduling_slot_offers": int(getattr(offers, "rowcount", 0) or 0),
            "scheduling_requests": len(requests),
            "scheduling_reminders": int(getattr(reminders, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (
            await db.execute(
                select(SchedulingRequest).where(SchedulingRequest.candidate_id == ctx.candidate_id)
            )
        ).scalars()
        out = []
        for req in rows:
            offers = (
                await db.execute(select(SchedulingSlotOffer).where(SchedulingSlotOffer.request_id == req.id))
            ).scalars()
            out.append(
                {
                    "request": req.human_id, "status": req.status, "time_zone": req.candidate_timezone, "created_at": req.created_at,
                    "offers": [
                        {"round": o.round, "status": o.status, "slots": o.slots, "picked_index": o.picked_index,
                         "suggested_times": o.counter_windows, "note": o.candidate_note}
                        for o in offers
                    ],
                }
            )  # fmt: skip
        return {"scheduling_requests": out}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        problems: list[str] = []
        if (
            await db.execute(
                select(SchedulingSlotOffer.id)
                .where(
                    SchedulingSlotOffer.candidate_id.in_(ids), SchedulingSlotOffer.candidate_note.is_not(None)
                )
                .limit(1)
            )
        ).first():
            problems.append("scheduling offer notes remain")
        if (
            await db.execute(
                select(SchedulingReminder.id).where(SchedulingReminder.candidate_id.in_(ids)).limit(1)
            )
        ).first():
            problems.append("scheduling reminders remain")
        return problems


register_erasure_handler(SchedulingRequestsHandler())
