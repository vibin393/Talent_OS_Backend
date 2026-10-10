"""Candidate facts and writes the portal domain (Epic 9) needs, exposed by the candidates domain (AD-1).

The portal never writes candidate tables itself: career applications (create from confirmed fields, or add a
resume version to an existing candidate -- parsed data stored, **never applied**), the candidate's own
profile edits and resume uploads, and the reads its allow-list projections are built from go through here.
Every function takes the caller's tenant session.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.candidates import (
    PARSE_STATUSES,
    Candidate,
    CandidateEmail,
    CandidateLink,
    CandidatePhone,
    CandidateResume,
)
from app.models.files import StoredFile
from app.schemas.candidates import (
    CandidateCreate,
    CandidateSkillIn,
    CandidateUpdate,
    EmailIn,
    LinkIn,
    LocationIn,
    PhoneIn,
)
from app.services import ai, files_service
from app.services.candidates import activity_port, duplicates, parsing, records
from app.services.candidates.common import normalize_email, normalize_phone, normalize_url
from app.services.candidates.extraction import Extraction, extract_text

PORTAL_SOURCE = "career_portal"
RESUME_OWNER = parsing.RESUME_FILE_OWNER


# ================================================================ helpers
def extract(data: bytes) -> Extraction:
    return extract_text(data, files_service.sniff_kind(data) or "unknown")


def clean_email(value: str) -> str | None:
    return normalize_email(value)


def clean_phone(value: str) -> str | None:
    return normalize_phone(value)


def clean_url(value: str) -> str | None:
    return normalize_url(value)


async def link_by_exact_email(ctx: RequestContext, db: AsyncSession, email: str) -> uuid.UUID | None:
    return await duplicates.link_by_exact_email(ctx, db, email)


# ================================================================ status / sign-in lookups
async def status(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any] | None:
    """``{full_name, merged, erased}`` or None when the candidate does not exist in this tenant."""
    row = await db.get(Candidate, candidate_id)
    if row is None:
        return None
    return {
        "full_name": row.full_name,
        "merged": row.merged_into_id is not None,
        "erased": row.erased_at is not None,
        "updated_at": row.updated_at,
    }


async def candidates_for_email(db: AsyncSession, email: str) -> list[dict[str, Any]]:
    """Live (not merged, not erased) candidates holding exactly this email: ``[{id, updated_at}]``."""
    norm = normalize_email(email)
    if norm is None:
        return []
    rows = await db.execute(
        select(Candidate.id, Candidate.updated_at)
        .join(CandidateEmail, CandidateEmail.candidate_id == Candidate.id)
        .where(
            CandidateEmail.email == norm, Candidate.merged_into_id.is_(None), Candidate.erased_at.is_(None)
        )
        .distinct()
    )
    return [{"id": cid, "updated_at": at} for cid, at in rows.all()]


async def primary_email(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    rows = (
        await db.execute(
            select(CandidateEmail.email, CandidateEmail.is_primary)
            .where(CandidateEmail.candidate_id == candidate_id)
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
        )
    ).all()
    return str(rows[0][0]) if rows else None


# ================================================================ profile
async def profile(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    cand = await db.get(Candidate, candidate_id)
    if cand is None:
        raise NotFound("Candidate not found")
    emails = (
        await db.execute(
            select(CandidateEmail.email)
            .where(CandidateEmail.candidate_id == candidate_id)
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
        )
    ).scalars()
    phones = (
        await db.execute(
            select(CandidatePhone.phone_e164)
            .where(CandidatePhone.candidate_id == candidate_id)
            .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
        )
    ).scalars()
    links = (
        await db.execute(
            select(CandidateLink.url_norm)
            .where(CandidateLink.candidate_id == candidate_id)
            .order_by(CandidateLink.created_at)
        )
    ).scalars()
    return {
        "full_name": cand.full_name,
        "emails": [str(e) for e in emails],
        "phones": list(phones),
        "links": list(links),
        "location": cand.location,
        "current_title": cand.current_title,
        "notice_period_days": cand.notice_period_days,
        "expected_ctc_amount": cand.expected_ctc_amount,
        "expected_ctc_currency": cand.expected_ctc_currency,
        "expected_ctc_unit": cand.expected_ctc_unit,
        "willing_to_relocate": cand.willing_to_relocate,
        "work_authorization": cand.work_authorization,
    }


PROFILE_SCALARS = (
    "full_name",
    "location",
    "current_title",
    "notice_period_days",
    "expected_ctc_amount",
    "expected_ctc_currency",
    "expected_ctc_unit",
    "willing_to_relocate",
    "work_authorization",
)


async def update_profile(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, fields: dict[str, Any]
) -> list[str]:
    """Candidate self-service edit (actor = the candidate on ``ctx``). Only the portal-editable fields;
    ``phones`` / ``links`` (lists of strings) replace the existing rows. Returns the changed field names."""
    cand = (
        await db.execute(select(Candidate).where(Candidate.id == candidate_id).with_for_update())
    ).scalar_one_or_none()
    if cand is None:
        raise NotFound("Candidate not found")
    changed: list[str] = []
    for field in PROFILE_SCALARS:
        if field in fields and getattr(cand, field) != fields[field]:
            setattr(cand, field, fields[field])
            changed.append(field)
    children: dict[str, Any] = {}
    if "phones" in fields:
        children["phones"] = [
            PhoneIn(phone=p, is_primary=i == 0) for i, p in enumerate(fields["phones"] or [])
        ]
    if "links" in fields:
        children["links"] = [LinkIn(url=u) for u in fields["links"] or []]
    if children:
        await records._replace_children(ctx, db, cand, CandidateUpdate(**children))
        changed += sorted(children)
    await db.flush()
    if changed:
        await activity_port.add_system_entry(
            ctx, db, "candidate", cand.id, "Updated by candidate", fields=changed
        )
        await duplicates.find_duplicates(ctx, db, cand)
    return changed


# ================================================================ resumes
async def add_resume(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    file_id: uuid.UUID,
    text: str | None,
    text_status: str,
    parse_status: str,
    parsed_data: dict[str, Any] | None,
    ai_action_id: uuid.UUID | None,
    note: str = "Resume uploaded",
) -> uuid.UUID:
    """Return a new resume version for ``candidate_id``. ``parsed_data`` is stored for recruiters to review
    as a diff
    (Epic 3); it is never applied to the profile here."""
    rid = uuid.uuid4()
    resume = CandidateResume(
        id=rid,
        tenant_id=ctx.tenant_id,
        candidate_id=candidate_id,
        file_id=file_id,
        version=await parsing.next_version(db, candidate_id),
        extracted_text=text,
        text_status=text_status,
        parse_status=parse_status if parse_status in PARSE_STATUSES else "failed_manual",
        ai_action_id=ai_action_id,
        parsed_data=parsed_data,
        uploaded_by=None,
    )
    db.add(resume)
    await db.flush()
    await files_service.set_file_owner(db, file_id, RESUME_OWNER, rid)
    if ai_action_id is not None:
        await ai.set_subject(db, ai_action_id, "candidate", candidate_id)
    await activity_port.add_system_entry(
        ctx, db, "candidate", candidate_id, note, resume_id=str(rid), version=resume.version
    )
    return rid


async def upload_resume(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    *,
    parse: bool,
) -> uuid.UUID:
    """Candidate portal upload: scanned store, text extraction, AI parse only when ``parse`` (the caller checks
    the AI-processing consent). Stored as a new version; parsed data is never auto-applied."""
    rid = uuid.uuid4()
    file = await files_service.store_upload(
        ctx, db, data, filename, files_service.RESUME_KINDS, RESUME_OWNER, rid
    )
    extraction = extract(data)
    parse_status, parsed, action_id = "skipped_consent", None, None
    if parse:
        if extraction.status == "ok" and extraction.text:
            parse_status, output, action_id, _ = await parsing.run_parse(
                ctx, db, extraction.text, subject=("candidate", candidate_id), input_ref=f"resume:{rid}"
            )
            parsed = output.model_dump(mode="json") if output is not None else None
        else:
            parse_status = "failed_manual"
    resume = CandidateResume(
        id=rid,
        tenant_id=ctx.tenant_id,
        candidate_id=candidate_id,
        file_id=file.id,
        version=await parsing.next_version(db, candidate_id),
        extracted_text=extraction.text,
        text_status=extraction.status,
        parse_status=parse_status,
        ai_action_id=action_id,
        parsed_data=parsed,
        uploaded_by=None,
    )
    db.add(resume)
    await db.flush()
    await activity_port.add_system_entry(
        ctx,
        db,
        "candidate",
        candidate_id,
        "Resume uploaded by candidate",
        resume_id=str(rid),
        version=resume.version,
    )
    return rid


async def resumes(db: AsyncSession, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(CandidateResume, StoredFile)
            .join(StoredFile, StoredFile.id == CandidateResume.file_id)
            .where(CandidateResume.candidate_id == candidate_id, StoredFile.deleted_at.is_(None))
            .order_by(CandidateResume.version.desc().nulls_last(), CandidateResume.created_at.desc())
        )
    ).all()
    return [
        {
            "id": r.id,
            "version": r.version,
            "file_name": f.original_name,
            "size_bytes": f.size_bytes,
            "content_type": f.content_type,
            "uploaded_at": r.created_at,
            "file_id": f.id,
            "storage_key": f.storage_key,
            "scan_status": f.scan_status,
        }
        for r, f in rows
    ]


async def resume_file(db: AsyncSession, candidate_id: uuid.UUID, resume_id: uuid.UUID) -> StoredFile:
    """Return the candidate's own resume file (else 404)."""
    row = await db.get(CandidateResume, resume_id)
    if row is None or row.candidate_id != candidate_id:
        raise NotFound("Resume not found")
    file = await files_service.get_file(db, row.file_id)
    if (
        file is None
        or file.deleted_at is not None
        or file.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES
    ):
        raise NotFound("Resume not found")
    return file


# ================================================================ career application: new candidate
async def create_from_application(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    full_name: str,
    email: str | None,
    phone: str | None,
    location: str | None,
    links: list[str],
    prefill: dict[str, Any] | None,
    source: str = PORTAL_SOURCE,
    source_detail: str | None = None,
) -> uuid.UUID:
    """Create a ``career_portal`` candidate from the confirmed fields plus the parsed profile facts (skills
    with ``source=parsed``), open duplicate review items (never merges) and return its id. Epic 70 (D-18,
    additive): board applicants pass ``source="job_board"`` and ``source_detail=<board key>``."""
    pre = prefill or {}
    data = CandidateCreate(
        full_name=full_name,
        source=source,
        source_detail=source_detail,
        emails=[EmailIn(email=email, is_primary=True)] if email else [],
        phones=[PhoneIn(phone=phone, is_primary=True)] if phone else [],
        links=[LinkIn(url=u) for u in links],
        location=LocationIn(city=location[:200]) if location else None,
        current_title=(pre.get("current_title") or None),
        current_employer=(pre.get("current_employer") or None),
        total_experience_months=pre.get("total_experience_months"),
        skills=[CandidateSkillIn(name=s[:200], source="parsed") for s in pre.get("skills") or [] if s],
    )
    cand, _ = await records.create_record(
        ctx, db, data, owner_id=owner_id, source=source, skill_origin="career_portal"
    )
    await duplicates.find_duplicates(ctx, db, cand)
    return cand.id


async def record_parse_decision(
    ctx: RequestContext,
    db: AsyncSession,
    action_id: uuid.UUID,
    prefill: dict[str, Any],
    submitted: dict[str, Any],
) -> str:
    """Epic 3 AI decision for a confirmed prefill: ``accepted`` when the parsed contact fields were used
    unchanged, else ``edited``."""
    same = all(
        (str(prefill.get(k) or "").strip().lower() == str(submitted.get(k) or "").strip().lower())
        for k in ("full_name", "email", "phone")
        if prefill.get(k)
    )
    decision = "accepted" if same else "edited"
    await ai.record_decision(ctx, db, action_id, decision)
    return decision
