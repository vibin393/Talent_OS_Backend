# ruff: noqa: E501
"""Public API inbound data loops (unified platform hub ingest).

* ``create_assessment`` (Croar -> ATS): resolve the Candidate in the tenant silo (by id or external_id =
  human_id); 422 if not found (an assessment NEVER creates a candidate). Optionally link an Application.
  Persist to ``candidate_assessment``. Idempotent on ``event_id``.
* ``create_engagement`` (Feedback IQ -> ATS): best-effort match the subject to a Candidate by email;
  STORE even when unmatched. Persist to ``engagement_signal``. Idempotent on ``event_id``.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed
from app.models.candidates import Candidate, CandidateEmail
from app.models.ingest import CandidateAssessment, EngagementSignal
from app.models.pipeline import Application
from app.schemas.public_v1.ingest import AssessmentIngestIn, CandidateRef, EngagementIngestIn
from app.services.public_api._core import base, ctx_principal


def _num(value: float | Decimal | None) -> float | None:
    return float(value) if value is not None else None


# ================================================================ assessments
async def _resolve_candidate(db: AsyncSession, ref: CandidateRef) -> Candidate:
    """Resolve a Candidate in the tenant silo by id (preferred), external_id (its human id), or email.

    Email is the key a cross-system producer that does not know the ATS id/human_id can still supply
    (e.g. Croar scores an assessment for a candidate it sourced by email). RLS scopes the lookup to the
    caller's tenant; a missing / out-of-scope ref is a 422 (an assessment never auto-creates a candidate)."""
    if ref.id is not None:
        stmt = select(Candidate).where(Candidate.id == ref.id)
    elif ref.external_id:
        stmt = select(Candidate).where(Candidate.human_id == ref.external_id)
    elif ref.email:
        matched_id = await _match_by_email(db, ref.email)
        if matched_id is None:
            raise ValidationFailed(
                "No candidate matches candidate_ref", code="candidate_not_found",
                details={"fields": {"candidate_ref": "Unknown candidate"}},
            )
        stmt = select(Candidate).where(Candidate.id == matched_id)
    else:
        raise ValidationFailed(
            "candidate_ref must include id, external_id or email",
            code="candidate_ref_required",
            details={"fields": {"candidate_ref": "Required"}},
        )
    row = (
        await db.execute(stmt.where(Candidate.erased_at.is_(None), Candidate.merged_into_id.is_(None)))
    ).scalars().first()
    if row is None:
        raise ValidationFailed(
            "No candidate matches candidate_ref", code="candidate_not_found",
            details={"fields": {"candidate_ref": "Unknown candidate"}},
        )
    return row


async def _resolve_application(db: AsyncSession, candidate_id: uuid.UUID, app_id: uuid.UUID | None) -> uuid.UUID | None:
    if app_id is None:
        return None
    row = (
        await db.execute(select(Application).where(Application.id == app_id, Application.candidate_id == candidate_id))
    ).scalars().first()
    if row is None:
        raise ValidationFailed(
            "No application matches application_ref for this candidate", code="application_not_found",
            details={"fields": {"application_ref": "Unknown application"}},
        )
    return row.id


def _assessment_out(row: CandidateAssessment) -> dict[str, Any]:
    return {
        **base(row, "candidate_assessment"),
        "event_id": row.event_id,
        "candidate_id": row.candidate_id,
        "application_id": row.application_id,
        "type": row.type,
        "provider": row.provider,
        "name": row.name,
        "score": _num(row.score),
        "max_score": _num(row.max_score),
        "status": row.status,
        "completed_at": row.completed_at,
        "detail": row.detail or {},
        "masked_fields": [],
    }


async def create_assessment(db: AsyncSession, data: AssessmentIngestIn) -> dict[str, Any]:
    _, principal = ctx_principal()
    tenant_id = principal.tenant_id
    existing = (
        await db.execute(select(CandidateAssessment).where(CandidateAssessment.event_id == data.event_id))
    ).scalars().first()
    if existing is not None:  # idempotent replay of the same source event
        return _assessment_out(existing)
    candidate = await _resolve_candidate(db, data.candidate_ref)
    application_id = await _resolve_application(
        db, candidate.id, data.application_ref.id if data.application_ref else None
    )
    a = data.assessment
    row = CandidateAssessment(
        tenant_id=tenant_id,
        event_id=data.event_id,
        candidate_id=candidate.id,
        application_id=application_id,
        type=a.type,
        provider=a.provider,
        name=a.name,
        score=Decimal(str(a.score)) if a.score is not None else None,
        max_score=Decimal(str(a.max_score)) if a.max_score is not None else None,
        status=a.status,
        completed_at=a.completed_at,
        detail=a.detail or {},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _assessment_out(row)


# ================================================================ engagement
def _engagement_out(row: EngagementSignal) -> dict[str, Any]:
    return {
        **base(row, "engagement_signal"),
        "event_id": row.event_id,
        "subject_email": row.subject_email,
        "subject_ats_candidate_id": row.subject_ats_candidate_id,
        "matched_candidate_id": row.matched_candidate_id,
        "matched": row.matched_candidate_id is not None,
        "type": row.type,
        "source": row.source,
        "score": _num(row.score),
        "summary": row.summary,
        "occurred_at": row.occurred_at,
        "detail": row.detail or {},
        "masked_fields": [],
    }


async def _match_by_email(db: AsyncSession, email: str | None) -> uuid.UUID | None:
    if not email:
        return None
    normalised = email.strip().lower()
    if not normalised:
        return None
    # CandidateEmail.email is CITEXT, so the comparison is case-insensitive; prefer the primary row.
    rows = (
        await db.execute(
            select(CandidateEmail.candidate_id, CandidateEmail.is_primary).where(CandidateEmail.email == normalised)
        )
    ).all()
    if not rows:
        return None
    for cid, primary in rows:
        if primary:
            return uuid.UUID(str(cid))
    return uuid.UUID(str(rows[0][0]))


async def create_engagement(db: AsyncSession, data: EngagementIngestIn) -> dict[str, Any]:
    _, principal = ctx_principal()
    tenant_id = principal.tenant_id
    existing = (
        await db.execute(select(EngagementSignal).where(EngagementSignal.event_id == data.event_id))
    ).scalars().first()
    if existing is not None:  # idempotent replay of the same source event
        return _engagement_out(existing)
    matched = await _match_by_email(db, data.subject_ref.email)
    s = data.signal
    row = EngagementSignal(
        tenant_id=tenant_id,
        event_id=data.event_id,
        subject_email=(data.subject_ref.email or None),
        subject_ats_candidate_id=(data.subject_ref.ats_candidate_id or None),
        matched_candidate_id=matched,
        type=s.type,
        source=s.source,
        score=Decimal(str(s.score)) if s.score is not None else None,
        summary=s.summary,
        occurred_at=s.occurred_at,
        detail=s.detail or {},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _engagement_out(row)


__all__ = ["create_assessment", "create_engagement"]
