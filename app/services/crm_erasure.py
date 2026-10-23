# ruff: file-ignore[line-too-long]
"""CRM erasure handler ``crm.leads`` (Epic 15 A8, AD-34).

A Lead's contact person may be the erasure subject (matched by the subject's email addresses). Their contact
fields (name, title, email, phone) are anonymised; the company-level Lead stays (no retention class). Runs
before ``candidates.profile`` (order 5) because that handler deletes the candidate's email rows.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidates import CandidateEmail
from app.models.crm import Lead
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _emails(db: AsyncSession, ctx: ErasureContext) -> list[str]:
    ids = await subject_ids(db, ctx.candidate_id)
    rows = await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id.in_(ids)))
    return [e.lower() for e in rows.scalars() if e]


async def _leads(db: AsyncSession, ctx: ErasureContext) -> list[Lead]:
    emails = await _emails(db, ctx)
    if not emails:
        return []
    rows = await db.execute(select(Lead).where(func.lower(Lead.contact_email).in_(emails)))
    retained = ctx.retained.get("leads", set())
    return [lead for lead in rows.scalars() if lead.id not in retained]


class CrmLeadsHandler:
    name = "crm.leads"
    order = 5
    tables = ("leads",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        leads = await _leads(db, ctx)
        for lead in leads:
            lead.contact_name = ERASED
            lead.contact_title = None
            lead.contact_email = None
            lead.contact_phone = None
        await db.flush()
        return {"leads": len(leads)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        leads = await _leads(db, ctx)
        if not leads:
            return None
        return {
            "leads": [
                {"human_id": lead.human_id, "company_name": lead.company_name, "contact_name": lead.contact_name,
                 "contact_title": lead.contact_title, "contact_email": lead.contact_email, "contact_phone": lead.contact_phone}
                for lead in leads
            ]
        }  # fmt: skip

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        return [f"leads:{lead.id}" for lead in await _leads(db, ctx)]


register_erasure_handler(CrmLeadsHandler())
