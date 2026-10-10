"""Immigration erasure handler ``immigration.cases`` (61), Epic 46 A6 (AD-34).

On Candidate erasure: dependants are deleted (their document requests cascade), receipt / permit numbers are
nulled with their last 4 and blind index, free-text notes become ``[erased]``, timeline payloads / actors are
blanked. Case and Permit shells (status, dates, country, visa type) are kept for the retention class
``immigration``. Documents are erased by the P2 documents handler (``worker_candidate_id`` = the Candidate).
``export`` lists the Candidate's cases, requested items and permits (never numbers).
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.immigration import CaseDependant, CaseDocumentRequest, CaseEvent, ImmigrationCase, Permit
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


class ImmigrationHandler:
    name = "immigration.cases"
    order = 61
    tables = ("immigration_cases", "case_events", "case_dependants", "case_document_requests", "permits")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        cases = list(
            (await db.execute(select(ImmigrationCase).where(ImmigrationCase.candidate_id.in_(ids)))).scalars()
        )
        case_ids = [c.id for c in cases]
        counts = {"cases": len(cases), "dependants": 0, "permits": 0, "requests": 0}
        for c in cases:
            c.receipt_number = None
            c.receipt_number_last4 = None
            c.outcome_note = ERASED if c.outcome_note else None
            c.withdraw_reason = ERASED if c.withdraw_reason else None
        if case_ids:
            deps = await db.execute(delete(CaseDependant).where(CaseDependant.case_id.in_(case_ids)))
            counts["dependants"] = int(getattr(deps, "rowcount", 0) or 0)
            await db.execute(
                update(CaseEvent).where(CaseEvent.case_id.in_(case_ids)).values(payload={}, actor={})
            )
        for r in (
            await db.execute(select(CaseDocumentRequest).where(CaseDocumentRequest.candidate_id.in_(ids)))
        ).scalars():
            r.rejection_reason = None
            counts["requests"] += 1
        for p in (await db.execute(select(Permit).where(Permit.candidate_id.in_(ids)))).scalars():
            p.number = None
            p.number_last4 = None
            p.number_bidx = None
            p.conditions = None
            counts["permits"] += 1
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        cases = list(
            (
                await db.execute(
                    select(ImmigrationCase).where(ImmigrationCase.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not cases:
            return None
        out = []
        for c in cases:
            reqs = (
                await db.execute(select(CaseDocumentRequest).where(CaseDocumentRequest.case_id == c.id))
            ).scalars()
            permits = (await db.execute(select(Permit).where(Permit.case_id == c.id))).scalars()
            out.append(
                {
                    "reference": c.human_id,
                    "country": c.country,
                    "status": c.status,
                    "stage": c.stage_key,
                    "opened_at": c.created_at,
                    "closed_at": c.closed_at,
                    "requested_documents": [{"item": r.label, "status": r.status} for r in reqs],
                    "permits": [
                        {"type": p.permit_type, "valid_from": p.valid_from, "valid_to": p.valid_to}
                        for p in permits
                    ],
                }
            )
        return {"immigration_cases": out}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for c in (
            await db.execute(select(ImmigrationCase).where(ImmigrationCase.candidate_id.in_(ids)))
        ).scalars():
            if c.receipt_number is not None:
                findings.append(f"receipt number remains on {c.human_id}")
            if (
                await db.execute(select(CaseDependant.id).where(CaseDependant.case_id == c.id))
            ).first() is not None:
                findings.append(f"dependants remain on {c.human_id}")
        if (
            await db.execute(
                select(Permit.id).where(Permit.candidate_id.in_(ids), Permit.number.is_not(None))
            )
        ).first() is not None:
            findings.append("permit numbers remain")
        return findings


register_erasure_handler(ImmigrationHandler())
