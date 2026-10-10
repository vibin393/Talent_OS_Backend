"""Candidate-domain erasure handlers (Epic 12, FR-68): ``candidates.profile`` (10), ``candidates.merges`` (20).

Every change goes through the ORM so the audit listener records it (old personal values encrypted under the
candidate's key, shredded last by ``audit.shred``). Candidates merged into the erased candidate are erased
with it (they hold the same person's data).
"""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateDuplicate,
    CandidateEducation,
    CandidateEmail,
    CandidateEmployment,
    CandidateLink,
    CandidateListMember,
    CandidateMerge,
    CandidatePhone,
    CandidateSkill,
    CandidateTag,
    ImportItem,
)
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED_NAME = "Erased candidate"
ERASED = "[erased]"
NULLED_FIELDS = (
    "location",
    "current_title",
    "current_employer",
    "total_experience_months",
    "current_ctc_amount",
    "current_ctc_currency",
    "current_ctc_unit",
    "expected_ctc_amount",
    "expected_ctc_currency",
    "expected_ctc_unit",
    "availability_date",
    "notice_period_days",
    "last_working_date",
    "work_authorization",
    "willing_to_relocate",
    "preferred_locations",
    "source_detail",
)
CHILD_MODELS: tuple[type[Any], ...] = (
    CandidateEmail,
    CandidatePhone,
    CandidateLink,
    CandidateEmployment,
    CandidateEducation,
    CandidateCertification,
    CandidateSkill,
    CandidateTag,
    CandidateListMember,
)


async def subject_ids(db: AsyncSession, candidate_id: uuid.UUID) -> list[uuid.UUID]:
    """Return the candidate plus every record merged into it."""
    merged = (await db.execute(select(Candidate.id).where(Candidate.merged_into_id == candidate_id))).scalars()
    return [candidate_id, *merged]


def _row_dict(row: Any, exclude: tuple[str, ...] = ("tenant_id",)) -> dict[str, Any]:
    return {c.key: getattr(row, c.key) for c in row.__table__.columns if c.key not in exclude and c.key != "text_tsv"}


class CandidateProfileHandler:
    name = "candidates.profile"
    order = 10
    tables = (
        "candidates",
        "candidate_emails",
        "candidate_phones",
        "candidate_links",
        "candidate_employments",
        "candidate_educations",
        "candidate_certifications",
        "candidate_skills",
        "candidate_tags",
        "candidate_list_members",
        "import_items",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        counts: dict[str, int] = {"candidates": 0}
        for model in CHILD_MODELS:
            rows = list((await db.execute(select(model).where(model.candidate_id.in_(ids)))).scalars())
            for row in rows:
                await db.delete(row)
            counts[model.__tablename__] = len(rows)
        items = list((await db.execute(select(ImportItem).where(ImportItem.candidate_id.in_(ids)))).scalars())
        for item in items:
            item.filename = ERASED
        counts["import_items"] = len(items)
        at = now()
        for cand in (await db.execute(select(Candidate).where(Candidate.id.in_(ids)))).scalars():
            cand.full_name = ERASED_NAME
            for field in NULLED_FIELDS:
                setattr(cand, field, None)
            cand.erased_at = cand.erased_at or at
            cand.archived_at = cand.archived_at or at
            cand.ai_consent_state = "withdrawn"
            counts["candidates"] += 1
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        cand = await db.get(Candidate, ctx.candidate_id)
        if cand is None:
            return None
        out: dict[str, Any] = {"profile": _row_dict(cand)}
        for model in CHILD_MODELS:
            rows = (await db.execute(select(model).where(model.candidate_id == ctx.candidate_id))).scalars()
            out[model.__tablename__] = [_row_dict(r, ("tenant_id", "candidate_id")) for r in rows]
        return out

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for cand in (await db.execute(select(Candidate).where(Candidate.id.in_(ids)))).scalars():
            if cand.full_name != ERASED_NAME or cand.erased_at is None:
                findings.append(f"candidate {cand.id} not anonymised")
            findings += [f"candidate {cand.id}.{f} not cleared" for f in NULLED_FIELDS if getattr(cand, f) is not None]
        for model in CHILD_MODELS:
            if (await db.execute(select(model.id).where(model.candidate_id.in_(ids)).limit(1))).first():
                findings.append(f"{model.__tablename__} rows remain")
        leftover = await db.execute(
            select(ImportItem.id).where(ImportItem.candidate_id.in_(ids), ImportItem.filename != ERASED).limit(1)
        )
        if leftover.first():
            findings.append("import_items filenames remain")
        return findings


class CandidateMergesHandler:
    name = "candidates.merges"
    order = 20
    tables = ("candidate_merges", "candidate_duplicates")

    @staticmethod
    def _merges(ids: list[uuid.UUID]) -> Any:
        return select(CandidateMerge).where(or_(CandidateMerge.survivor_id.in_(ids), CandidateMerge.merged_id.in_(ids)))

    @staticmethod
    def _dups(ids: list[uuid.UUID]) -> Any:
        return select(CandidateDuplicate).where(
            or_(CandidateDuplicate.candidate_a_id.in_(ids), CandidateDuplicate.candidate_b_id.in_(ids))
        )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        merges = list((await db.execute(self._merges(ids))).scalars())
        for m in merges:
            m.snapshot = {}
            m.field_choices = {}
        dups = list((await db.execute(self._dups(ids))).scalars())
        for d in dups:
            d.reasons = []
        await db.flush()
        return {"candidate_merges": len(merges), "candidate_duplicates": len(dups)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # noqa: ARG002
        return None  # merge-undo snapshots duplicate the profile export

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings = [f"merge {m.id} snapshot remains" for m in (await db.execute(self._merges(ids))).scalars() if m.snapshot or m.field_choices]
        findings += [f"duplicate {d.id} reasons remain" for d in (await db.execute(self._dups(ids))).scalars() if d.reasons]
        return findings


register_erasure_handler(CandidateProfileHandler())
register_erasure_handler(CandidateMergesHandler())

__all__ = ["CandidateMergesHandler", "CandidateProfileHandler", "subject_ids"]
