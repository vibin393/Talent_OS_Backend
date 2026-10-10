"""AI-domain erasure handler ``ai.actions`` (60) (Epic 12, FR-68).

For actions whose subject is the candidate, or whose ``input_ref`` points at one of the candidate's resumes
(``resume:<id>`` / ``import_item:<id>``): ``output`` and ``limitations_note`` -> NULL and ``input_ref`` ->
``erased``. Metrics (tokens, latency, model, decision) are kept for usage and governance reporting.
"""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIAction
from app.services.candidates import privacy_port as candidates
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED_REF = "erased"
REF_PREFIXES = ("resume", "import_item")


async def _stmt(db: AsyncSession, candidate_id: uuid.UUID) -> Any:
    ids = await subject_ids(db, candidate_id)
    refs: list[str] = []
    for cid in ids:
        for rid in await candidates.resume_ids(db, cid):
            refs += [f"{prefix}:{rid}" for prefix in REF_PREFIXES]
    cond = (AIAction.subject_type == "candidate") & AIAction.subject_id.in_(ids)
    return select(AIAction).where(or_(cond, AIAction.input_ref.in_(refs)) if refs else cond)


class AIActionsHandler:
    name = "ai.actions"
    order = 60
    tables = ("ai_actions",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        rows = list((await db.execute(await _stmt(db, ctx.candidate_id))).scalars())
        for row in rows:
            row.output = None
            row.limitations_note = None
            row.input_ref = ERASED_REF
        await db.flush()
        return {"ai_actions": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (await db.execute(await _stmt(db, ctx.candidate_id))).scalars()
        return {
            "ai_actions": [
                {
                    "feature": r.feature,
                    "created_at": r.created_at,
                    "model": r.model,
                    "status": r.status,
                    "output": r.output,
                    "decision": r.decision,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        rows = (await db.execute(await _stmt(db, ctx.candidate_id))).scalars()
        return [
            f"ai_action {r.id} output remains"
            for r in rows
            if r.output is not None or r.limitations_note is not None or r.input_ref != ERASED_REF
        ]


register_erasure_handler(AIActionsHandler())
