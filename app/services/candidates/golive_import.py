"""Go-live Worker import (Epic 35 A8, FR-185): ``find_or_create_for_import``, a P1 addition for ``golive``.

Matches an existing Candidate by email, then by phone (never merges); otherwise creates one (source
``bulk_import``) and records ``engagement`` consent with source ``import``. ``validate_only`` writes nothing.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
from app.schemas.candidates import CandidateCreate, EmailIn, PhoneIn
from app.services.candidates.common import normalize_email, normalize_phone, principal_of


def _get(row: Mapping[str, Any] | Any, key: str) -> str:
    value = row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)
    return str(value).strip() if value is not None else ""


def _field(column: str, message: str, code: str) -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": [{"field": column, "message": message}]})


async def _match(db: AsyncSession, email: str | None, phone: str | None) -> uuid.UUID | None:
    live = (Candidate.merged_into_id.is_(None),)
    if email:
        found = (
            await db.execute(
                select(Candidate.id)
                .join(CandidateEmail, CandidateEmail.candidate_id == Candidate.id)
                .where(CandidateEmail.email == email, *live)
                .order_by(Candidate.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
    if phone:
        return (
            await db.execute(
                select(Candidate.id)
                .join(CandidatePhone, CandidatePhone.candidate_id == Candidate.id)
                .where(CandidatePhone.phone_e164 == phone, *live)
                .order_by(Candidate.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    return None


async def find_or_create_for_import(
    ctx: RequestContext, db: AsyncSession, row: Mapping[str, Any] | Any, *, validate_only: bool = False
) -> dict[str, Any]:
    """``row``: ``full_name``, ``email`` and/or ``phone``, optional ``current_title`` / ``current_employer``.

    Returns ``{"candidate_id": uuid | None, "created": bool, "matched_by": "email" | "phone" | None}``
    (``candidate_id`` is ``None`` only when ``validate_only`` and the Worker would be created).
    Raises ``ValidationFailed`` (``details.fields[].field`` = the CSV column) on bad input.
    """
    raw_email, raw_phone, name = _get(row, "email"), _get(row, "phone"), _get(row, "full_name")
    email = normalize_email(raw_email) if raw_email else None
    if raw_email and email is None:
        raise _field("email", "Invalid email address", "invalid_email")
    phone = normalize_phone(raw_phone) if raw_phone else None
    if raw_phone and phone is None:
        raise _field("phone", "Invalid phone number", "invalid_phone")
    if not email and not phone:
        raise _field("email", "An email or phone is required", "required")
    found = await _match(db, email, phone)
    if found is not None:
        by = "email" if email and found == await _match(db, email, None) else "phone"
        return {"candidate_id": found, "created": False, "matched_by": by}
    if not name:
        raise _field("full_name", "Full name is required for a new Worker", "required")
    if len(name) > 200:
        raise _field("full_name", "Full name is too long", "too_long")
    title = _get(row, "current_title") or None
    employer = _get(row, "current_employer") or None
    data = CandidateCreate(
        full_name=name,
        source="bulk_import",
        emails=[EmailIn(email=email, is_primary=True)] if email else [],
        phones=[PhoneIn(phone=phone, is_primary=True)] if phone else [],
        current_title=title[:200] if title else None,
        current_employer=employer[:200] if employer else None,
    )
    if validate_only:
        return {"candidate_id": None, "created": False, "matched_by": None}
    from app.services.candidates.records import create_record
    from app.services.privacy.consent import grant_consent

    cand, _warnings = await create_record(ctx, db, data, owner_id=principal_of(ctx).id, source="bulk_import")
    await grant_consent(ctx, db, cand.id, "engagement", source="import", evidence={"via": "golive_import"})
    return {"candidate_id": cand.id, "created": True, "matched_by": None}
