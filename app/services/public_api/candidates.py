# ruff: file-ignore[line-too-long]
"""Public Candidates (Epic 67, spec A4): the domain filter builder ``candidates.records.list_stmt`` (its scope predicate
``read_condition``) for lists, ``create_candidate`` / ``update_candidate`` / ``parsing.upload_resume`` for writes.
Contact fields need ``pii.contact:read``; compensation ``pii.financial:read`` (and the user's ``compensation:view``)."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.errors import ValidationFailed
from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
from app.schemas.candidates import CandidateCreate, CandidateFilters, CandidateUpdate, EmailIn, PhoneIn
from app.schemas.public_v1.resources import CandidateCreateIn, CandidateUpdateIn
from app.services.candidates import parsing, records
from app.services.candidates.common import visible_stmt
from app.services.public_api._core import (
    Order,
    acting_owner,
    base,
    ctx_principal,
    expose,
    finish_all,
    money,
    one,
    page,
)

OBJECT = "candidate"


async def _contacts(db: AsyncSession, ids: Sequence[uuid.UUID]) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    if not ids:
        return {}, {}
    emails: dict[uuid.UUID, str] = {}
    for cid, email, primary in (await db.execute(select(CandidateEmail.candidate_id, CandidateEmail.email, CandidateEmail.is_primary).where(CandidateEmail.candidate_id.in_(ids)))).all():
        if primary or cid not in emails:
            emails[cid] = email
    phones: dict[uuid.UUID, str] = {}
    for cid, phone, primary in (await db.execute(select(CandidatePhone.candidate_id, CandidatePhone.phone_e164, CandidatePhone.is_primary).where(CandidatePhone.candidate_id.in_(ids)))).all():
        if primary or cid not in phones:
            phones[cid] = phone
    return emails, phones


def _out(c: Candidate, emails: dict[uuid.UUID, str], phones: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        **base(c, OBJECT, human_id=c.human_id),
        "full_name": c.full_name,
        "email": emails.get(c.id),
        "phone": phones.get(c.id),
        "location": c.location if isinstance(c.location, dict) else None,
        "current_title": c.current_title,
        "current_employer": c.current_employer,
        "total_experience_months": c.total_experience_months,
        "notice_period_days": c.notice_period_days,
        "availability_date": c.availability_date,
        "current_ctc": money(c.current_ctc_amount, c.current_ctc_currency),
        "expected_ctc": money(c.expected_ctc_amount, c.expected_ctc_currency),
        "source": c.source,
        "owner_id": c.owner_id,
        "archived": c.archived_at is not None,
    }


async def _build(db: AsyncSession, rows: Sequence[Candidate]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    emails, phones = await _contacts(db, [r.id for r in rows])
    await expose(principal, "candidate", [r.id for r in rows])
    return finish_all(principal, [_out(r, emails, phones) for r in rows])


async def list_candidates(
    db: AsyncSession, *, limit: int, cursor: str | None, order: Order, updated_since: datetime | None = None,
    created_since: datetime | None = None, source: str | None = None, owner_id: uuid.UUID | None = None, tag: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    _, principal = ctx_principal()
    filters = CandidateFilters.model_construct(q=None, owner_id=owner_id, tag_id=tag, list_id=None, source=source,  # type: ignore[arg-type]
                                               ai_consent_state=None, include_archived=True, sort="updated_at")  # fmt: skip
    stmt = records.list_stmt(principal, filters).order_by(None).where(Candidate.erased_at.is_(None))
    if updated_since is not None:
        stmt = stmt.where(Candidate.updated_at >= updated_since)
    if created_since is not None:
        stmt = stmt.where(Candidate.created_at >= created_since)
    return await page(db, stmt, sort_col=Candidate.updated_at, id_col=Candidate.id, limit=limit, cursor=cursor, order=order,
                      build=lambda rows: _build(db, rows))  # fmt: skip


async def _row(db: AsyncSession, candidate_id: uuid.UUID) -> Candidate:
    _, principal = ctx_principal()
    stmt = visible_stmt(principal).where(Candidate.id == candidate_id, Candidate.merged_into_id.is_(None), Candidate.erased_at.is_(None))
    return await one(db, stmt)  # type: ignore[no-any-return]


async def get_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    return (await _build(db, [await _row(db, candidate_id)]))[0]


async def current_version(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    from app.services.public_api._core import version_of

    return version_of(await _row(db, candidate_id))


async def create_candidate(db: AsyncSession, data: CandidateCreateIn) -> dict[str, Any]:
    ctx, principal = ctx_principal()
    if data.consent is None:
        raise ValidationFailed(
            "A consent basis is required to add a candidate through the API",
            code="consent_basis_required",
            details={"fields": {"consent": "Required"}},
        )
    owner = acting_owner(principal, data.owner_id)
    cc = CandidateCreate(
        full_name=data.full_name,
        emails=[EmailIn(email=data.email, is_primary=True)] if data.email else [],
        phones=[PhoneIn(phone=data.phone, is_primary=True)] if data.phone else [],
        current_title=data.current_title,
        current_employer=data.current_employer,
        total_experience_months=data.total_experience_months,
        notice_period_days=data.notice_period_days,
        availability_date=data.availability_date,
        source="public_api",
        source_detail=data.source_detail,
        owner_id=owner,
    )
    out = await records.create_candidate(ctx, db, cc)
    cid = uuid.UUID(str(out["id"]))
    audit.record(
        "public_api.candidate_consent", "candidate", cid, session=db, basis=data.consent.basis,
        notice_version=data.consent.notice_version, captured_at=data.consent.captured_at.isoformat(), channel=data.consent.channel,
    )  # fmt: skip
    result = await get_candidate(db, cid)
    result["possible_duplicate"] = bool((out.get("warnings") or {}).get("possible_duplicates"))
    return result


async def update_candidate(db: AsyncSession, candidate_id: uuid.UUID, data: CandidateUpdateIn) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    await _row(db, candidate_id)
    update = CandidateUpdate.model_validate(data.model_dump(exclude_unset=True))
    await records.update_candidate(ctx, db, candidate_id, update)
    await db.flush()
    return await get_candidate(db, candidate_id)


async def upload_resume(db: AsyncSession, candidate_id: uuid.UUID, data: bytes, filename: str | None) -> dict[str, Any]:
    ctx, principal = ctx_principal()
    await _row(db, candidate_id)
    out = await parsing.upload_resume(ctx, db, candidate_id, data, filename)
    await expose(principal, "candidate", [candidate_id])
    payload = out.model_dump() if hasattr(out, "model_dump") else dict(out)
    resume = payload.get("resume") or payload
    return {"resume_id": resume.get("id"), "candidate_id": candidate_id, "status": str(resume.get("parse_status") or payload.get("status") or "uploaded")}


__all__ = ["create_candidate", "current_version", "get_candidate", "list_candidates", "update_candidate", "upload_resume"]
