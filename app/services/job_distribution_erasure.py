# ruff: noqa: E501
"""Job distribution erasure handlers (Epic 70, spec A4). A top-level module so ``privacy`` never imports the Phase 5
package.

* ``job_distribution.suppress`` (order 5 -- before ``candidates.profile`` (10) anonymises the contact rows): hashes
  of the erased Candidate's emails and phones (merged ids included) go to ``board_ingest_suppressions`` with reason
  ``erased``, so a board applicant with the same email / phone is never re-created.
* ``job_distribution.ingests`` (order 72): the subject's ``board_applicant_ingests`` rows keep their idempotency key
  but lose ``candidate_id`` / ``application_id`` (they never held personal data).
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_distribution import BoardApplicantIngest
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class JobBoardSuppressHandler:
    name = "job_distribution.suppress"
    order = 5
    tables: tuple[str, ...] = ()

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"suppressed": 0}
        from app.services.job_distribution import suppression

        total = 0
        for cid in await subject_ids(db, ctx.candidate_id):
            total += await suppression.add_candidate(db, cid, "erased")
        await db.flush()
        return {"suppressed": total}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:  # noqa: ARG002
        return []


class JobBoardIngestsHandler:
    name = "job_distribution.ingests"
    order = 72
    tables = ("board_applicant_ingests",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"ingests": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        res = await db.execute(update(BoardApplicantIngest).where(BoardApplicantIngest.candidate_id.in_(ids)).values(candidate_id=None, application_id=None))
        await db.flush()
        return {"ingests": int(getattr(res, "rowcount", 0) or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (await db.execute(select(BoardApplicantIngest).where(BoardApplicantIngest.candidate_id == ctx.candidate_id))).scalars()
        items = [{"board": r.board_key, "received_at": r.received_at, "status": r.status} for r in rows]
        return {"job_board_applications": items} if items else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        hit = (await db.execute(select(BoardApplicantIngest.id).where(BoardApplicantIngest.candidate_id.in_(ids)).limit(1))).first()
        return ["job board ingest rows still reference the candidate"] if hit is not None else []


register_erasure_handler(JobBoardSuppressHandler())
register_erasure_handler(JobBoardIngestsHandler())
