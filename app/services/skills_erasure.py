# ruff: noqa: E501
"""Skills erasure handler ``skills.evidence`` (8), Epic 37 A6 (AD-34).

Deletes the erased candidate's ``candidate_skill_evidence`` and ``skill_suggestions`` (both also cascade with
``candidate_skills`` / ``candidates``). Runs before the candidates handler (10). Retention follows the Candidate:
a retention erasure of the Candidate runs this handler too.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skills_graph import CandidateSkillEvidence, SkillSuggestion
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class SkillsHandler:
    name = "skills.evidence"
    order = 8
    tables = ("candidate_skill_evidence", "skill_suggestions")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        counts: dict[str, int] = {}
        for model, key in ((CandidateSkillEvidence, "evidence"), (SkillSuggestion, "suggestions")):
            result = await db.execute(delete(model).where(model.candidate_id.in_(ids)))
            counts[key] = int(result.rowcount or 0)  # type: ignore[attr-defined]
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        ev = list(
            (
                await db.execute(
                    select(CandidateSkillEvidence).where(
                        CandidateSkillEvidence.candidate_id == ctx.candidate_id
                    )
                )
            ).scalars()
        )
        sg = list(
            (
                await db.execute(
                    select(SkillSuggestion).where(SkillSuggestion.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not ev and not sg:
            return None
        return {
            "skill_evidence": [
                {
                    "kind": e.kind,
                    "proficiency": e.proficiency,
                    "years": e.years,
                    "last_used": e.last_used,
                    "note": e.note,
                    "observed_at": e.observed_at,
                }
                for e in ev
            ],
            "skill_suggestions": [
                {"source": s.source, "proposed_proficiency": s.proposed_proficiency, "status": s.status}
                for s in sg
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left: list[str] = []
        if (
            await db.execute(
                select(CandidateSkillEvidence.id).where(CandidateSkillEvidence.candidate_id.in_(ids)).limit(1)
            )
        ).first():
            left.append("skill evidence remains")
        if (
            await db.execute(select(SkillSuggestion.id).where(SkillSuggestion.candidate_id.in_(ids)).limit(1))
        ).first():
            left.append("skill suggestion remains")
        return left


register_erasure_handler(SkillsHandler())
