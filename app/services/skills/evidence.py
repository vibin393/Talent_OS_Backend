# ruff: noqa: E501
"""Candidate skill evidence (Epic 37 A2, FR-188, AD-41).

* ``add_evidence`` -- validates the kind / actor rule, inserts, recomputes. API kinds: ``recruiter`` (``skill:verify``
  on the Candidate and a human actor, 403 ``human_only_action`` otherwise) and ``self`` (``candidate:update``).
  System kinds (``resume``, ``graph``, ``project``, ``assessment``) come only from the handlers below.
* Handlers (after commit -> ``maintenance`` tasks, idempotent by ``source_ref``): ``CandidateSkillsChanged`` adds the
  baseline row to Candidate Skills without evidence; ``AssessmentScored`` adds ``assessment`` evidence or a
  suggestion; ``ProjectAssignmentEnded`` adds ``project`` evidence or a suggestion (role skills from P1 data only).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, is_human, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.skill_refs import SkillKey
from app.models.candidates import CandidateSkill
from app.models.skills_graph import CandidateSkillEvidence
from app.schemas.skills import EvidenceIn, EvidenceOut, SkillProfileRow
from app.services.candidates import common as cand_common
from app.services.skills import graph as graph_mod
from app.services.skills.recompute import recompute

API_KINDS = frozenset({"recruiter", "self"})
SYSTEM_KINDS = frozenset({"resume", "graph", "project", "assessment"})


def _now() -> datetime:
    return datetime.now(UTC)


def _out(
    row: CandidateSkillEvidence, after: tuple[int | None, str] | None = None, name: str | None = None
) -> EvidenceOut:
    return EvidenceOut(
        id=row.id,
        candidate_skill_id=row.candidate_skill_id,
        kind=row.kind,  # type: ignore[arg-type]
        proficiency=row.proficiency,
        years=row.years,
        last_used=row.last_used,
        source_ref=row.source_ref,
        note=row.note,
        observed_at=row.observed_at,
        created_by_kind=row.created_by_kind,
        created_by_id=row.created_by_id,
        created_by_name=name,
        superseded_at=row.superseded_at,
        proficiency_after=after[0] if after else None,
        verification_after=after[1] if after else None,
    )


async def _skill_of(
    db: AsyncSession, candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID
) -> CandidateSkill:
    row = await db.get(CandidateSkill, candidate_skill_id)
    if row is None or row.candidate_id != candidate_id:
        raise NotFound("Candidate skill not found")
    return row


async def insert_evidence(
    ctx: RequestContext, db: AsyncSession, skill: CandidateSkill, data: EvidenceIn
) -> tuple[CandidateSkillEvidence, tuple[int | None, str] | None, bool]:
    """Insert (idempotent by ``(candidate_skill, kind, source_ref)``) and recompute. Returns (row, after, created)."""
    if data.source_ref:
        existing = (
            await db.execute(
                select(CandidateSkillEvidence).where(
                    CandidateSkillEvidence.candidate_skill_id == skill.id,
                    CandidateSkillEvidence.kind == data.kind,
                    CandidateSkillEvidence.source_ref == data.source_ref,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, None, False
    actor = actor_from_ctx(ctx)
    row = CandidateSkillEvidence(
        id=uuid.uuid4(),
        tenant_id=skill.tenant_id,
        candidate_skill_id=skill.id,
        candidate_id=skill.candidate_id,
        kind=data.kind,
        proficiency=data.proficiency,
        years=data.years,
        last_used=data.last_used,
        source_ref=data.source_ref,
        note=data.note,
        observed_at=_now(),
        created_by_kind=actor.kind,
        created_by_id=actor.id,
    )
    db.add(row)
    await db.flush()
    after = await recompute(db, skill.id)
    return row, after, True


async def add_evidence(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_skill_id: uuid.UUID,
    data: EvidenceIn,
    *,
    candidate_id: uuid.UUID | None = None,
) -> EvidenceOut:
    """Public API (``skills_service.add_evidence``): validates the kind / actor rule, inserts, recomputes."""
    skill = await db.get(CandidateSkill, candidate_skill_id)
    if skill is None or (candidate_id is not None and skill.candidate_id != candidate_id):
        raise NotFound("Candidate skill not found")
    actor = actor_from_ctx(ctx)
    if data.kind == "recruiter":
        require_human(actor)
        if actor.kind != "user":
            raise ValidationFailed("Only a User can verify a skill", code="evidence_kind_not_allowed")
        await cand_common.get_for(ctx, db, skill.candidate_id, "skill:verify")
    elif data.kind == "self":
        if actor.kind == "candidate":
            if actor.id != skill.candidate_id:
                raise NotFound("Candidate skill not found")
        else:
            await cand_common.get_editable(ctx, db, skill.candidate_id)
    elif is_human(actor):
        raise ValidationFailed(
            "This evidence kind is recorded automatically",
            code="evidence_kind_not_allowed",
            details={"fields": [{"field": "kind", "message": "Use recruiter or self"}]},
        )
    row, after, created = await insert_evidence(ctx, db, skill, data)
    if created and data.kind == "recruiter":
        audit.record(
            "candidate_skill.verified",
            "candidate",
            skill.candidate_id,
            session=db,
            candidate_skill_id=str(skill.id),
            evidence_id=str(row.id),
            proficiency=data.proficiency,
        )
    return _out(row, after)


async def list_evidence(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID
) -> list[EvidenceOut]:
    await cand_common.get_visible(ctx, db, candidate_id)
    await _skill_of(db, candidate_id, candidate_skill_id)
    rows = list(
        (
            await db.execute(
                select(CandidateSkillEvidence)
                .where(CandidateSkillEvidence.candidate_skill_id == candidate_skill_id)
                .order_by(CandidateSkillEvidence.observed_at.desc(), CandidateSkillEvidence.id)
            )
        ).scalars()
    )
    names = await cand_common.user_names(
        db, {r.created_by_id for r in rows if r.created_by_id and r.created_by_kind == "user"}
    )
    return [_out(r, name=names.get(r.created_by_id) if r.created_by_id else None) for r in rows]


async def supersede(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    candidate_skill_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> EvidenceOut:
    require_human(actor_from_ctx(ctx))
    await cand_common.get_for(ctx, db, candidate_id, "skill:verify")
    await _skill_of(db, candidate_id, candidate_skill_id)
    row = await db.get(CandidateSkillEvidence, evidence_id)
    if row is None or row.candidate_skill_id != candidate_skill_id:
        raise NotFound("Evidence not found")
    if row.superseded_at is None:
        row.superseded_at = _now()
        await db.flush()
        audit.record(
            "candidate_skill.evidence_superseded",
            "candidate",
            candidate_id,
            session=db,
            candidate_skill_id=str(candidate_skill_id),
            evidence_id=str(evidence_id),
        )
    after = await recompute(db, candidate_skill_id)
    return _out(row, after)


async def profile(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[SkillProfileRow]:
    await cand_common.get_visible(ctx, db, candidate_id)
    counts = dict(
        (
            await db.execute(
                select(CandidateSkillEvidence.candidate_skill_id, func.count())
                .where(
                    CandidateSkillEvidence.candidate_id == candidate_id,
                    CandidateSkillEvidence.superseded_at.is_(None),
                )
                .group_by(CandidateSkillEvidence.candidate_skill_id)
            )
        ).all()
    )
    rows = list(
        (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    )
    keys = {graph_mod.ref_key(r.skill_id, r.tenant_skill_id) for r in rows}
    names = await graph_mod.labels(db, keys)
    out = [
        SkillProfileRow(
            candidate_skill_id=r.id,
            skill_key=graph_mod.ref_key(r.skill_id, r.tenant_skill_id),
            name=names[graph_mod.ref_key(r.skill_id, r.tenant_skill_id)].name,
            proficiency=r.proficiency,
            verification=r.verification,  # type: ignore[arg-type]
            evidence_count=int(counts.get(r.id, 0)),
        )
        for r in rows
    ]
    out.sort(key=lambda p: p.name.lower())
    return out


# ================================================================ handlers (system context)
async def ensure_baseline(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> int:
    """``CandidateSkillsChanged``: baseline evidence for every Candidate Skill without evidence; never deletes."""
    has = select(CandidateSkillEvidence.id).where(
        CandidateSkillEvidence.candidate_skill_id == CandidateSkill.id
    )
    rows = list(
        (
            await db.execute(
                select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id, ~has.exists())
            )
        ).scalars()
    )
    for r in rows:
        await insert_evidence(
            ctx,
            db,
            r,
            EvidenceIn(
                kind="resume" if r.source == "parsed" else "self", years=r.years, last_used=r.last_used
            ),
        )
    return len(rows)


async def find_candidate_skill(
    db: AsyncSession, candidate_id: uuid.UUID, key: SkillKey
) -> CandidateSkill | None:
    col = CandidateSkill.skill_id if key.kind == "global" else CandidateSkill.tenant_skill_id
    return (
        await db.execute(
            select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id, col == key.id)
        )
    ).scalar_one_or_none()


async def on_assessment_scored(ctx: RequestContext, db: AsyncSession, event: Any) -> dict[str, int]:
    from app.services.skills import inference

    ref = f"assessment_invitation:{event.invitation_id}"
    added = suggested = 0
    for raw_key, proficiency in event.skill_scores:
        try:
            key = SkillKey.parse(str(raw_key))
        except ValueError:
            continue
        level = int(proficiency) if proficiency is not None and 1 <= int(proficiency) <= 5 else None
        skill = await find_candidate_skill(db, event.candidate_id, key)
        if skill is not None:
            _, _, created = await insert_evidence(
                ctx, db, skill, EvidenceIn(kind="assessment", proficiency=level, source_ref=ref)
            )
            added += int(created)
        elif await inference.suggest(
            ctx,
            db,
            event.candidate_id,
            key,
            source="assessment",
            proficiency=level,
            rationale="Scored in an assessment",
            source_ref=ref,
        ):
            suggested += 1
    return {"evidence": added, "suggestions": suggested}


async def _role_skill_rows(db: AsyncSession, event: Any) -> list[Any]:
    """Role skills from P1 data only: the Placement's Demand, else the Demands created from the Project Role."""
    from app.models.demands import Demand, DemandSkill
    from app.models.placements import Placement

    demand_ids: list[uuid.UUID] = []
    if event.placement_id is not None:
        did = (
            await db.execute(select(Placement.demand_id).where(Placement.id == event.placement_id))
        ).scalar_one_or_none()
        if did is not None:
            demand_ids = [did]
    if not demand_ids and event.project_role_id is not None:
        demand_ids = list(
            (
                await db.execute(select(Demand.id).where(Demand.project_role_id == event.project_role_id))
            ).scalars()
        )
    if not demand_ids:
        return []
    rows = await db.execute(
        select(DemandSkill.skill_id, DemandSkill.skill_ref_kind, DemandSkill.min_proficiency)
        .where(DemandSkill.demand_id.in_(demand_ids), DemandSkill.skill_id.is_not(None))
        .order_by(DemandSkill.position)
    )
    seen: dict[tuple[str, uuid.UUID], Any] = {}
    for r in rows:
        k = (r.skill_ref_kind or "global", r.skill_id)
        if k not in seen or (r.min_proficiency or 0) > (seen[k].min_proficiency or 0):
            seen[k] = r
    return list(seen.values())


async def on_project_assignment_ended(ctx: RequestContext, db: AsyncSession, event: Any) -> dict[str, int]:
    from app.services.skills import inference

    ref = f"project_assignment:{event.assignment_id}"
    added = suggested = 0
    for r in await _role_skill_rows(db, event):
        key = SkillKey(kind="tenant" if r.skill_ref_kind == "tenant" else "global", id=r.skill_id)
        skill = await find_candidate_skill(db, event.candidate_id, key)
        if skill is not None:
            _, _, created = await insert_evidence(
                ctx, db, skill, EvidenceIn(kind="project", proficiency=r.min_proficiency, source_ref=ref)
            )
            added += int(created)
        elif await inference.suggest(
            ctx,
            db,
            event.candidate_id,
            key,
            source="project",
            proficiency=r.min_proficiency,
            rationale="Used on a project assignment",
            source_ref=ref,
        ):
            suggested += 1
    return {"evidence": added, "suggestions": suggested}
