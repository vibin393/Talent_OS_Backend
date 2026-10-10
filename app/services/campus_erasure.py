# ruff: noqa: E501
"""Campus erasure handler ``campus.records`` (57), Epic 43 A8 (FR-258, AD-34).

* Registrations, event check-ins and queue entries of the subject (and records merged into it) are deleted; batch
  slots pointing at those registrations are released (``registration_id`` nulled, the P1 interview is handled by the
  interviews handler); answer files are tombstoned. Funnel counts are recomputed from the remaining rows (no
  snapshot is kept).
* Import invites carrying the subject's email are minimised.
* ``institution_contacts`` holds placement officers (not candidates); a contact is erased through the institution UI
  (archive + the PII fields are audit-encrypted). The handler claims the table for the coverage guard and erases a
  contact whose email equals the subject's email.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campus import (
    BatchSlot,
    EventCheckin,
    InstitutionContact,
    QueueEntry,
    Registration,
    RegistrationInvite,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _emails(db: AsyncSession, ids: list[Any]) -> list[str]:
    from app.models.candidates import CandidateEmail

    return [str(e) for e in (await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id.in_(ids)))).scalars()]


class CampusHandler:
    name = "campus.records"
    order = 57
    tables: tuple[str, ...] = (
        "registrations",
        "registration_invites",
        "event_checkins",
        "queue_entries",
        "batch_slots",
        "institution_contacts",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        from app.services import files_service

        ids = await subject_ids(db, ctx.candidate_id)
        emails = await _emails(db, ids)
        regs = list((await db.execute(select(Registration).where(Registration.candidate_id.in_(ids)))).scalars())
        reg_ids = [r.id for r in regs]
        counts = {"registrations": len(regs), "checkins": 0, "queue_entries": 0, "invites": 0, "contacts": 0}
        for r in regs:
            for value in (r.answers or {}).values():
                if isinstance(value, str) and len(value) == 36:
                    try:
                        import uuid

                        await files_service.purge_file(db, uuid.UUID(value))
                    except (ValueError, TypeError):
                        continue
        if reg_ids:
            await db.execute(update(BatchSlot).where(BatchSlot.registration_id.in_(reg_ids)).values(registration_id=None, interview_id=None))
            await db.execute(update(RegistrationInvite).where(RegistrationInvite.registration_id.in_(reg_ids)).values(registration_id=None))
            await db.execute(delete(Registration).where(Registration.id.in_(reg_ids)))
        res = await db.execute(delete(QueueEntry).where(QueueEntry.candidate_id.in_(ids)))
        counts["queue_entries"] = int(getattr(res, "rowcount", 0) or 0)
        res = await db.execute(delete(EventCheckin).where(EventCheckin.candidate_id.in_(ids)))
        counts["checkins"] = int(getattr(res, "rowcount", 0) or 0)
        if emails:
            res = await db.execute(
                update(RegistrationInvite).where(RegistrationInvite.email.in_(emails)).values(email=None, name=None, prefill={}, status="expired")
            )
            counts["invites"] = int(getattr(res, "rowcount", 0) or 0)
            res = await db.execute(
                update(InstitutionContact)
                .where(InstitutionContact.email.in_(emails))
                .values(name=ERASED, email=None, phone=None, title=None, is_primary=False)
            )
            counts["contacts"] = int(getattr(res, "rowcount", 0) or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        regs = list((await db.execute(select(Registration).where(Registration.candidate_id == ctx.candidate_id))).scalars())
        checkins = list((await db.execute(select(EventCheckin).where(EventCheckin.candidate_id == ctx.candidate_id))).scalars())
        if not regs and not checkins:
            return None
        return {
            "campus_registrations": [
                {
                    "drive_id": str(r.drive_id),
                    "status": r.status,
                    "answers": r.answers,
                    "registered_at": r.registered_at,
                    "eligibility": r.eligibility,
                    "score": str(r.score) if r.score is not None else None,
                }
                for r in regs
            ],
            "event_checkins": [
                {"event_id": str(c.event_id), "status": c.status, "registered_at": c.registered_at, "checked_in_at": c.checked_in_at}
                for c in checkins
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        if (await db.execute(select(Registration.id).where(Registration.candidate_id.in_(ids)).limit(1))).first():
            findings.append("campus registrations remain")
        if (await db.execute(select(EventCheckin.id).where(EventCheckin.candidate_id.in_(ids)).limit(1))).first():
            findings.append("event check-ins remain")
        if (await db.execute(select(QueueEntry.id).where(QueueEntry.candidate_id.in_(ids)).limit(1))).first():
            findings.append("queue entries remain")
        return findings


register_erasure_handler(CampusHandler())
