"""Candidate facts and the screening field write-back the pipeline domain (Epic 6) needs, exposed by the
candidates domain (AD-1). Pipeline code never queries candidate tables itself.

* ``summaries`` -- display facts for pipeline rows (no contact details).
* ``contact`` -- primary email (for secure-link invitations and rejection emails).
* ``screening_fields`` / ``update_candidate_fields`` -- the fields screening answers may propose, and the
  explicit, audited write-back after the recruiter confirmed it (FR-34).
* ``get_resume_profile`` / ``get_primary_resume_file`` -- inputs of the submission resume (FR-36).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateEducation,
    CandidateEmail,
    CandidateEmployment,
    CandidateLink,
    CandidatePhone,
    CandidateResume,
    CandidateSkill,
    Skill,
    TenantSkill,
)
from app.services.candidates.common import get_editable

SCREENING_FIELDS: tuple[str, ...] = (
    "expected_ctc",
    "notice_period_days",
    "current_location",
    "work_authorization",
    "willing_to_relocate",
)


async def exists_visible(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.core.errors import DomainError
    from app.services.candidates.common import get_visible

    try:
        await get_visible(ctx, db, candidate_id)
    except DomainError:
        return False
    return True


async def summaries(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Return ``{id: {human_id, full_name, first_name, current_title, city, ...}}`` display facts."""
    id_list = list(dict.fromkeys(ids))
    if not id_list:
        return {}
    rows = (await db.execute(select(Candidate).where(Candidate.id.in_(id_list)))).scalars()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        loc = r.location if isinstance(r.location, dict) else {}
        out[r.id] = {
            "human_id": r.human_id,
            "full_name": r.full_name,
            "first_name": (r.full_name or "").split(" ")[0] or None,
            "current_title": r.current_title,
            "current_employer": r.current_employer,
            "city": loc.get("city"),
            "total_experience_months": r.total_experience_months,
            "notice_period_days": r.notice_period_days,
            "location": loc,
            "erased": r.erased_at is not None,
            "archived": r.archived_at is not None,
        }
    return out


async def primary_email(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    row = (
        await db.execute(
            select(CandidateEmail.email)
            .where(CandidateEmail.candidate_id == candidate_id)
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(row) if row else None


async def contact_strings(db: AsyncSession, candidate_id: uuid.UUID) -> list[str]:
    """Emails, phones and URLs on record (used to verify a generated resume holds none of them)."""
    out: list[str] = []
    out += [
        str(e)
        for e in (
            await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id == candidate_id))
        ).scalars()
    ]
    out += [
        str(p)
        for p in (
            await db.execute(
                select(CandidatePhone.phone_e164).where(CandidatePhone.candidate_id == candidate_id)
            )
        ).scalars()
    ]
    out += [
        str(u)
        for u in (
            await db.execute(select(CandidateLink.url_norm).where(CandidateLink.candidate_id == candidate_id))
        ).scalars()
    ]
    return out


# ================================================================ screening write-back
def _money(amount: Decimal | None, currency: str | None, unit: str | None) -> dict[str, Any] | None:
    if amount is None:
        return None
    return {"amount": str(amount), "currency": currency, "unit": unit}


async def screening_fields(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    row = await db.get(Candidate, candidate_id)
    if row is None:
        return {}
    loc = row.location if isinstance(row.location, dict) else None
    return {
        "expected_ctc": _money(row.expected_ctc_amount, row.expected_ctc_currency, row.expected_ctc_unit),
        "notice_period_days": row.notice_period_days,
        "current_location": (loc or {}).get("city") if loc else None,  # NOSONAR
        "work_authorization": row.work_authorization,
        "willing_to_relocate": row.willing_to_relocate,
    }


async def update_candidate_fields(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, values: dict[str, Any]
) -> list[str]:
    """Write confirmed screening values (``candidate:update`` in scope, else 403/404). ORM -> audited."""
    cand = await get_editable(ctx, db, candidate_id, for_update=True)
    applied: list[str] = []
    for field, value in values.items():
        if field not in SCREENING_FIELDS:
            raise ValidationFailed("Unknown candidate field", details={"fields": {field: "Not updatable"}})
        if field == "expected_ctc":
            if isinstance(value, dict):
                amount = value.get("amount")
                currency = value.get("currency")
            else:
                amount, currency = value, None
            try:
                cand.expected_ctc_amount = Decimal(str(amount)) if amount is not None else None
            except Exception as exc:
                raise ValidationFailed(
                    "Invalid amount", details={"fields": {field: "Invalid amount"}}
                ) from exc
            if currency:
                cand.expected_ctc_currency = str(currency)[:3].upper()
            elif cand.expected_ctc_currency is None:
                cand.expected_ctc_currency = None
        elif field == "notice_period_days":
            cand.notice_period_days = int(value) if value is not None else None
        elif field == "current_location":
            loc = dict(cand.location) if isinstance(cand.location, dict) else {}
            loc["city"] = str(value)[:200] if value else None
            cand.location = loc
        elif field == "work_authorization":
            cand.work_authorization = str(value)[:200] if value is not None else None
        elif field == "willing_to_relocate":
            cand.willing_to_relocate = bool(value) if value is not None else None
        applied.append(field)
    await db.flush()
    return applied


# ================================================================ resume inputs
async def get_primary_resume_file(db: AsyncSession, candidate_id: uuid.UUID) -> uuid.UUID | None:
    """``stored_files.id`` of the candidate's latest resume version (None when there is none)."""
    row = (
        await db.execute(
            select(CandidateResume.file_id)
            .where(CandidateResume.candidate_id == candidate_id)
            .order_by(CandidateResume.version.desc().nullslast(), CandidateResume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def get_resume_profile(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any] | None:
    """Structured profile for a client-formatted resume, or None when the latest resume is not parsed.

    Holds no contact data by construction (no emails, phones, links, address): headline, summary, skills,
    experience, education, certifications. Free text still goes through the PDF scrubber.
    """
    cand = await db.get(Candidate, candidate_id)
    if cand is None:
        return None
    latest = (
        await db.execute(
            select(CandidateResume)
            .where(CandidateResume.candidate_id == candidate_id)
            .order_by(CandidateResume.version.desc().nullslast(), CandidateResume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or latest.parse_status != "parsed":
        return None
    employments = (
        await db.execute(
            select(CandidateEmployment)
            .where(CandidateEmployment.candidate_id == candidate_id)
            .order_by(
                CandidateEmployment.is_current.desc(), CandidateEmployment.start_date.desc().nullslast()
            )
        )
    ).scalars()
    educations = (
        await db.execute(
            select(CandidateEducation)
            .where(CandidateEducation.candidate_id == candidate_id)
            .order_by(CandidateEducation.end_year.desc().nullslast())
        )
    ).scalars()
    certs = (
        await db.execute(
            select(CandidateCertification).where(CandidateCertification.candidate_id == candidate_id)
        )
    ).scalars()
    skill_rows = (
        await db.execute(
            select(CandidateSkill.years, Skill.name, TenantSkill.name)
            .outerjoin(Skill, Skill.id == CandidateSkill.skill_id)
            .outerjoin(TenantSkill, TenantSkill.id == CandidateSkill.tenant_skill_id)
            .where(CandidateSkill.candidate_id == candidate_id)
        )
    ).all()
    months = cand.total_experience_months
    loc = cand.location if isinstance(cand.location, dict) else {}
    headline = " at ".join(x for x in (cand.current_title, cand.current_employer) if x) or None
    return {
        "full_name": cand.full_name,
        "headline": headline,
        "summary": None,
        "experience_years": round(months / 12, 1) if months else None,
        "notice_period_days": cand.notice_period_days,
        "city": loc.get("city"),
        "skills": [
            {"name": g or t, "years": float(y) if y is not None else None}
            for y, g, t in skill_rows
            if (g or t)
        ],
        "experience": [
            {
                "title": e.title,
                "employer": e.employer,
                "start": e.start_date.isoformat() if e.start_date else None,
                "end": e.end_date.isoformat() if e.end_date else None,
                "is_current": e.is_current,
                "description": e.description,
            }
            for e in employments
        ],
        "education": [
            {"degree": e.degree, "field": e.field, "institution": e.institution, "end_year": e.end_year}
            for e in educations
        ],
        "certifications": [{"name": c.name, "issuer": c.issuer} for c in certs],
    }
