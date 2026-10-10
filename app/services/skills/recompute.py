# ruff: noqa: E501
"""Proficiency / verification recompute (Epic 37 A2, FR-188, AD-41).

``derive(evidence)`` is the single, pure rule:

* any non-superseded **verified** evidence (``recruiter``, ``assessment``) -> ``verified``; proficiency = the max
  non-null verified proficiency;
* else any **declared** evidence (``self``) -> ``declared``; proficiency = the latest non-null declared value;
* else any **inferred** evidence (``resume``, ``graph``, ``project``) -> ``inferred``; latest non-null inferred value;
* no live evidence -> ``None`` (the caller keeps the foundation backfill: ``declared`` / ``inferred`` by P1
  ``source``, proficiency NULL).

``recompute`` is the only writer of ``candidate_skills.proficiency`` / ``verification`` and writes only on change.
"""

import dataclasses
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidates import CandidateSkill
from app.models.skills_graph import DECLARED_KINDS, INFERRED_KINDS, VERIFIED_KINDS, CandidateSkillEvidence


@dataclasses.dataclass(frozen=True, slots=True)
class Ev:
    kind: str
    proficiency: int | None
    observed_at: datetime
    superseded: bool = False


def _latest(items: list[Ev]) -> int | None:
    dated = sorted((e for e in items if e.proficiency is not None), key=lambda e: e.observed_at)
    return dated[-1].proficiency if dated else None


def derive(evidence: Iterable[Ev]) -> tuple[int | None, str] | None:
    live = [e for e in evidence if not e.superseded]
    verified = [e for e in live if e.kind in VERIFIED_KINDS]
    if verified:
        values = [e.proficiency for e in verified if e.proficiency is not None]
        return (max(values) if values else None), "verified"
    declared = [e for e in live if e.kind in DECLARED_KINDS]
    if declared:
        return _latest(declared), "declared"
    inferred = [e for e in live if e.kind in INFERRED_KINDS]
    if inferred:
        return _latest(inferred), "inferred"
    return None


def baseline_verification(source: str) -> str:
    return "inferred" if source == "parsed" else "declared"


async def recompute(db: AsyncSession, candidate_skill_id: uuid.UUID) -> tuple[int | None, str] | None:
    """Re-derive one Candidate Skill. Returns the stored ``(proficiency, verification)`` (None if gone)."""
    row = await db.get(CandidateSkill, candidate_skill_id, with_for_update=True)
    if row is None:
        return None
    evs = [
        Ev(r.kind, r.proficiency, r.observed_at, r.superseded_at is not None)
        for r in await db.execute(
            select(
                CandidateSkillEvidence.kind,
                CandidateSkillEvidence.proficiency,
                CandidateSkillEvidence.observed_at,
                CandidateSkillEvidence.superseded_at,
            ).where(CandidateSkillEvidence.candidate_skill_id == candidate_skill_id)
        )
    ]
    derived = derive(evs)
    proficiency, verification = derived if derived is not None else (None, baseline_verification(row.source))
    if (proficiency, verification) != (row.proficiency, row.verification):
        row.proficiency = proficiency
        row.verification = verification
        await db.flush()
    return proficiency, verification
