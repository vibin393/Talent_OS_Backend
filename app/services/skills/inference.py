# ruff: file-ignore[line-too-long]
"""Skill inference as reviewable suggestions (Epic 37 A3, FR-189, AD-6).

* **Graph implications** (always, no AI): for each ``verified`` / ``declared`` Candidate Skill X, every direct
  ``X implies Y`` edge (weight >= 0.6) whose Y the Candidate lacks becomes a ``graph`` suggestion.
* **AI inference** (feature ``skill_inference``, default off, and only when the Candidate's AI-processing consent
  allows -- checked *before* the gateway so no ``skipped`` ``ai_actions`` row is written): prompt
  ``skill_inference.v1`` over the latest resume text (untrusted) and the current skill names (trusted). Names
  resolve through P1 ``resolve_skill(create_pending=False)``; unresolved names are dropped.
* Decisions: accept (optionally with an edited proficiency) creates / reuses the Candidate Skill and adds evidence of
  kind ``resume`` / ``graph`` / ``project`` (class inferred -- never verified) or ``assessment`` (needs
  ``skill:verify`` and a human); reject. AI rows record ``ai_gateway.record_decision``.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.skill_refs import SkillKey
from app.models.candidates import Candidate, CandidateResume, CandidateSkill
from app.models.skills_graph import SkillSuggestion
from app.schemas.skills import EvidenceIn, SkillInferenceOut, SkillSuggestionOut
from app.services.candidates import common as cand_common
from app.services.skills import graph as graph_mod
from app.services.skills import settings as graph_settings
from app.services.skills.evidence import find_candidate_skill, insert_evidence

FEATURE = "skill_inference"
PROMPT_ID = "skill_inference.v1"
GRAPH_IMPLY_MIN = Decimal("0.6")
RESUME_CHARS = 60_000
EVIDENCE_KIND = {"ai_resume": "resume", "graph": "graph", "project": "project", "assessment": "assessment"}
# Epic 60 (C-10): interview suggestions -> ``interview`` (verified) or ``interview_declared`` (declared), decided by the
# source's registered evidence verifier at accept time.
EVIDENCE_KIND["interview"] = "interview"
DECLARED_KIND = {"interview": "interview_declared"}
EvidenceVerifier = Callable[[RequestContext, AsyncSession, SkillSuggestion], Awaitable[str]]
_VERIFIERS: dict[str, EvidenceVerifier] = {}


def register_evidence_verifier(source: str, fn: EvidenceVerifier) -> None:
    """Epic 60 (C-10): an owner registers ``fn(ctx, db, suggestion) -> "verified" | "declared"`` for a suggestion
    source; ``accept`` consults it. Without a verifier the source's evidence is recorded at the declared level."""
    _VERIFIERS[source] = fn


async def _evidence_kind(ctx: RequestContext, db: AsyncSession, row: SkillSuggestion) -> str:
    if row.source not in DECLARED_KIND:
        return EVIDENCE_KIND[row.source]
    # Interview evidence is accepted by a person only (the agent never accepts its own suggestions).
    require_human(actor_from_ctx(ctx))
    verifier = _VERIFIERS.get(row.source)
    level = await verifier(ctx, db, row) if verifier is not None else "declared"
    if level == "verified":
        await cand_common.get_for(ctx, db, row.candidate_id, "skill:verify")
        return EVIDENCE_KIND[row.source]
    return DECLARED_KIND[row.source]


def _now() -> datetime:
    return datetime.now(UTC)


def _key_of(row: Any) -> SkillKey:
    return (
        SkillKey(kind="global", id=row.skill_id)
        if row.skill_id
        else SkillKey(kind="tenant", id=row.tenant_skill_id)
    )


async def suggest(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    key: SkillKey,
    *,
    source: str,
    proficiency: int | None = None,
    rationale: str | None = None,
    source_ref: str | None = None,
    ai_action_id: uuid.UUID | None = None,
    resume_id: uuid.UUID | None = None,
) -> bool:
    """Create a pending suggestion unless the Candidate has the skill, one is pending, the same ``source_ref`` was
    already suggested, or the same (skill, source) was rejected before. Returns True when created."""
    if await find_candidate_skill(db, candidate_id, key) is not None:
        return False
    col = SkillSuggestion.skill_id if key.kind == "global" else SkillSuggestion.tenant_skill_id
    prior = list(
        (
            await db.execute(
                select(SkillSuggestion.status, SkillSuggestion.source, SkillSuggestion.source_ref).where(
                    SkillSuggestion.candidate_id == candidate_id, col == key.id
                )
            )
        ).all()
    )
    for status, src, ref in prior:
        if (
            status == "pending"
            or (source_ref and ref == source_ref)
            or (status == "rejected" and src == source)
        ):
            return False
    row = SkillSuggestion(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        candidate_id=candidate_id,
        skill_id=key.id if key.kind == "global" else None,
        tenant_skill_id=key.id if key.kind == "tenant" else None,
        proposed_proficiency=proficiency,
        source=source,
        rationale=(rationale or None) and rationale[:500],
        ai_action_id=ai_action_id,
        resume_id=resume_id,
        source_ref=source_ref,
        status="pending",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:  # a concurrent pending suggestion
        return False
    return True


# ================================================================ inference run
async def _graph_suggestions(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> int:
    rows = list(
        (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    )
    have = {graph_mod.ref_key(r.skill_id, r.tenant_skill_id) for r in rows}
    sources = [r for r in rows if r.verification in ("verified", "declared")]
    if not sources:
        return 0
    graph = await graph_mod.load(db, ctx.tenant_id)
    implied: dict[str, tuple[Decimal, str]] = {}
    for r in sources:
        src = graph_mod.ref_key(r.skill_id, r.tenant_skill_id)
        for edge in graph_mod.implied_by(graph, src):
            if edge.weight >= GRAPH_IMPLY_MIN and edge.dst not in have:
                cur = implied.get(edge.dst)
                if cur is None or edge.weight > cur[0]:
                    implied[edge.dst] = (edge.weight, src)
    if not implied:
        return 0
    names = await graph_mod.labels(db, set(implied) | {v[1] for v in implied.values()})
    created = 0
    for dst in sorted(implied):
        weight, src = implied[dst]
        if names[dst].status not in ("approved",):
            continue
        created += int(
            await suggest(
                ctx,
                db,
                candidate_id,
                SkillKey.parse(dst),
                source="graph",
                rationale=f"Implied by {names[src].name} ({weight:.1f})",
            )
        )
    return created


async def _consent_allows(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.services.ai import gateway

    checker = gateway._consent_checker
    decision = await checker(ctx, db, FEATURE, ("candidate", candidate_id))
    return bool(decision.allowed)


async def _ai_suggestions(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    from app.services.ai import gateway
    from app.services.candidates import skills as catalog

    if not await gateway.is_feature_enabled(db, FEATURE):
        return {"ai": "feature_disabled"}
    if not await _consent_allows(ctx, db, candidate_id):
        return {"ai": "consent_missing"}
    resume = (
        await db.execute(
            select(CandidateResume)
            .where(CandidateResume.candidate_id == candidate_id, CandidateResume.extracted_text.is_not(None))
            .order_by(CandidateResume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if resume is None or not (resume.extracted_text or "").strip():
        return {"ai": "no_resume"}
    rows = list(
        (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    )
    names = await graph_mod.labels(db, {graph_mod.ref_key(r.skill_id, r.tenant_skill_id) for r in rows})
    current = ", ".join(sorted(n.name for n in names.values())) or "(none)"
    result = await gateway.run(
        ctx,
        db,
        FEATURE,
        PROMPT_ID,
        {"current_skills": current[:4000]},
        {"document": (resume.extracted_text or "")[:RESUME_CHARS]},
        SkillInferenceOut,
        subject=("candidate", candidate_id),
        input_ref=f"candidate_resume:{resume.id}",
    )
    if not result.ok or not isinstance(result.data, SkillInferenceOut):
        return {"ai": result.error_code or "failed"}
    created = 0
    for item in result.data.skills[:40]:
        ref = await catalog.resolve_skill(ctx, db, item.name, create_pending=False)
        if ref is None or ref.status != "approved":
            continue
        created += int(
            await suggest(
                ctx,
                db,
                candidate_id,
                SkillKey(kind=ref.kind, id=ref.id),
                source="ai_resume",
                proficiency=item.proficiency,
                rationale=item.evidence_quote or "Found in the resume",
                ai_action_id=result.action_id,
                resume_id=resume.id,
                source_ref=f"ai_action:{result.action_id}",
            )
        )
    return {"ai": "ok", "ai_suggestions": created}


async def ai_wanted(db: AsyncSession) -> bool:
    from app.services.ai import gateway

    return await gateway.is_feature_enabled(db, FEATURE)


async def infer(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, graph: bool = True, ai: bool = True
) -> dict[str, Any]:
    """Graph implications (``graph``) and AI inference (``ai``, when enabled and consented). Idempotent."""
    cfg = await graph_settings.current(db)
    if not cfg.inference_auto_suggest:
        return {"skipped": "auto_suggest_off"}
    exists = (
        await db.execute(
            select(Candidate.id).where(
                Candidate.id == candidate_id,
                Candidate.merged_into_id.is_(None),
                Candidate.erased_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        return {"skipped": "candidate_gone"}
    out: dict[str, Any] = {}
    if graph:
        out["graph_suggestions"] = await _graph_suggestions(ctx, db, candidate_id)
    if ai:
        out |= await _ai_suggestions(ctx, db, candidate_id)
    return out


# ================================================================ listing and decisions
async def _out(db: AsyncSession, rows: list[SkillSuggestion]) -> list[SkillSuggestionOut]:
    names = await graph_mod.labels(db, {_key_of(r).as_str() for r in rows})
    return [
        SkillSuggestionOut(
            id=r.id,
            candidate_id=r.candidate_id,
            skill_key=_key_of(r).as_str(),
            name=names[_key_of(r).as_str()].name,
            proposed_proficiency=r.proposed_proficiency,
            source=r.source,
            rationale=r.rationale,
            status=r.status,
            created_at=r.created_at,
            decided_at=r.decided_at,
        )
        for r in rows
    ]


async def list_suggestions(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, status: str | None = "pending"
) -> list[SkillSuggestionOut]:
    await cand_common.get_visible(ctx, db, candidate_id)
    stmt = select(SkillSuggestion).where(SkillSuggestion.candidate_id == candidate_id)
    if status:
        stmt = stmt.where(SkillSuggestion.status == status)
    rows = list(
        (await db.execute(stmt.order_by(SkillSuggestion.created_at.desc(), SkillSuggestion.id))).scalars()
    )
    return await _out(db, rows)


async def _pending(db: AsyncSession, candidate_id: uuid.UUID, suggestion_id: uuid.UUID) -> SkillSuggestion:
    row = await db.get(SkillSuggestion, suggestion_id, with_for_update=True)
    if row is None or row.candidate_id != candidate_id:
        raise NotFound("Suggestion not found")
    if row.status != "pending":
        raise ConflictError("This suggestion was already decided", code="suggestion_decided")
    return row


async def accept(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    proficiency: int | None = None,
) -> SkillSuggestionOut:
    await cand_common.get_editable(ctx, db, candidate_id)
    row = await _pending(db, candidate_id, suggestion_id)
    if row.source == "assessment":
        require_human(actor_from_ctx(ctx))
        await cand_common.get_for(ctx, db, candidate_id, "skill:verify")
    evidence_kind = await _evidence_kind(ctx, db, row)  # Epic 60: interview -> verified / declared
    key = _key_of(row)
    skill = await find_candidate_skill(db, candidate_id, key)
    if skill is None:
        skill = CandidateSkill(
            id=uuid.uuid4(),
            tenant_id=row.tenant_id,
            candidate_id=candidate_id,
            skill_id=row.skill_id,
            tenant_skill_id=row.tenant_skill_id,
            source="manual" if row.source in ("assessment", "interview") else "parsed",
        )
        db.add(skill)
        await db.flush()
    level = proficiency if proficiency is not None else row.proposed_proficiency
    await insert_evidence(
        ctx,
        db,
        skill,
        EvidenceIn(
            kind=evidence_kind, proficiency=level, source_ref=f"skill_suggestion:{row.id}"
        ),
    )
    edited = proficiency is not None and proficiency != row.proposed_proficiency
    row.status = "edited" if edited else "accepted"
    row.decided_by = getattr(ctx.principal, "id", None)
    row.decided_at = _now()
    await db.flush()
    if row.ai_action_id is not None:
        from app.services.ai import gateway

        await gateway.record_decision(ctx, db, row.ai_action_id, row.status)
    audit.record(
        "skill_suggestion.accepted",
        "candidate",
        candidate_id,
        session=db,
        suggestion_id=str(row.id),
        skill_key=key.as_str(),
        source=row.source,
        proficiency=level,
    )
    return (await _out(db, [row]))[0]


async def reject(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    reason: str | None = None,
) -> SkillSuggestionOut:
    await cand_common.get_editable(ctx, db, candidate_id)
    row = await _pending(db, candidate_id, suggestion_id)
    row.status = "rejected"
    row.decided_by = getattr(ctx.principal, "id", None)
    row.decided_at = _now()
    await db.flush()
    if row.ai_action_id is not None:
        from app.services.ai import gateway

        await gateway.record_decision(ctx, db, row.ai_action_id, "rejected")
    audit.record(
        "skill_suggestion.rejected",
        "candidate",
        candidate_id,
        session=db,
        suggestion_id=str(row.id),
        skill_key=_key_of(row).as_str(),
        source=row.source,
        reason=reason,
    )
    return (await _out(db, [row]))[0]
