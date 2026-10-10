"""Resumes and AI parsing (FR-19, stories 3.3 / 3.4).

Upload -> ``files.store_upload`` (size, sniffing, scan) -> text extraction -> ``ai.run("resume_parsing")``
-> post-validation (emails validated, phones normalised, skills resolved without creating pending ones,
projects kept in ``parsed_data`` only). A human always confirms: a new candidate is created from the prefill
(``resume_id`` on create) and an existing one gets a field-level diff to apply. Each confirmation records
the AI decision: ``accepted`` (all parsed values used unchanged), ``edited`` (some), ``rejected`` (none).
AI failures never fail the upload: the resume is stored and ``parse_status`` tells the user why.
"""

import dataclasses
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
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
)
from app.schemas.candidates import (
    SCALAR_DIFF_FIELDS,
    CandidateCreate,
    CandidatePrefill,
    CandidateSkillIn,
    CertificationIn,
    DiffEntry,
    EducationIn,
    EmailIn,
    EmploymentIn,
    LinkIn,
    LocationIn,
    PhoneIn,
    ResumeApply,
    ResumeDraftOut,
    ResumeOut,
    ResumeParseOutput,
    ResumeUploadOut,
    SkillRef,
    SkillRefIn,
    UnresolvedSkill,
)
from app.services import ai, files_service
from app.services.candidates import activity_port, records, skills
from app.services.candidates.common import (
    get_editable,
    get_visible,
    link_kind,
    normalize_email,
    normalize_phone,
    normalize_url,
    principal_of,
)
from app.services.candidates.extraction import extract_text

FEATURE = "resume_parsing"
PROMPT_ID = "resume_parse"
RESUME_FILE_OWNER = "candidate_resume"

_AI_STATUS = {"feature_disabled": "skipped_disabled", "consent_missing": "skipped_consent"}


# ================================================================ output
async def resume_out(db: AsyncSession, row: CandidateResume) -> ResumeOut:
    file = await files_service.get_file(db, row.file_id)
    assert file is not None
    return ResumeOut(
        id=row.id,
        candidate_id=row.candidate_id,
        version=row.version,
        text_status=row.text_status,
        parse_status=row.parse_status,
        parse_label=records.PARSE_LABELS.get(row.parse_status, row.parse_status),
        ai_action_id=row.ai_action_id,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        file=files_service.file_out(file),
    )


# ================================================================ post-validation
@dataclasses.dataclass
class Parsed:
    """Normalised parse result (what the prefill / diff are built from)."""

    full_name: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    total_experience_months: int | None = None
    location: dict[str, str | None] | None = None
    emails: list[str] = dataclasses.field(default_factory=list)
    phones: list[str] = dataclasses.field(default_factory=list)
    links: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    skills: list[tuple[SkillRef, Decimal | None, date | None]] = dataclasses.field(default_factory=list)
    unresolved: list[UnresolvedSkill] = dataclasses.field(default_factory=list)
    employment: list[EmploymentIn] = dataclasses.field(default_factory=list)
    education: list[EducationIn] = dataclasses.field(default_factory=list)
    certifications: list[CertificationIn] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)


_DATE_RE = re.compile(r"^\s*(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    m = _DATE_RE.match(value)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return date(year, month, day)
    except ValueError:
        try:
            return date(year, month, 1)
        except ValueError:
            return None


def _clip(value: str | None, n: int) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value[:n] or None


def _years(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value)).quantize(Decimal("0.1"))
    except InvalidOperation:
        return None
    return d if Decimal(0) <= d <= Decimal(999) else None


async def postprocess(ctx: RequestContext, db: AsyncSession, out: ResumeParseOutput) -> Parsed:
    p = Parsed(
        full_name=_clip(out.full_name, 200),
        current_title=_clip(out.current_title, 200),
        current_employer=_clip(out.current_employer, 200),
        total_experience_months=(
            out.total_experience_months
            if out.total_experience_months is not None and 0 <= out.total_experience_months <= 1200
            else None
        ),
    )
    if out.location is not None:
        loc = {k: _clip(v, 200) for k, v in out.location.model_dump().items()}
        p.location = loc if any(loc.values()) else None
    for raw in out.emails:
        email = normalize_email(raw)
        if email is None:
            p.warnings.append("invalid_email_dropped")
        elif email not in p.emails:
            p.emails.append(email)
    for raw in out.phones:
        phone = normalize_phone(raw)
        if phone is None:
            p.warnings.append("invalid_phone_dropped")
        elif phone not in p.phones:
            p.phones.append(phone)
    for link in out.links:
        url = normalize_url(link.url)
        if url is None:
            p.warnings.append("invalid_link_dropped")
        elif url not in {u for u, _ in p.links}:
            p.links.append((url, link.kind or link_kind(url)))
    seen: set[tuple[str, uuid.UUID]] = set()
    for sk in out.skills:
        name = _clip(sk.name, 200)
        if not name:
            continue
        ref = await skills.resolve_skill(ctx, db, name, create_pending=False)
        if ref is None or ref.status == "rejected":
            if name.lower() not in {u.name.lower() for u in p.unresolved}:
                p.unresolved.append(
                    UnresolvedSkill(name=name, years=_years(sk.years), last_used=parse_date(sk.last_used))
                )
            continue
        if (ref.kind, ref.id) in seen:
            continue
        seen.add((ref.kind, ref.id))
        p.skills.append((ref, _years(sk.years), parse_date(sk.last_used)))
    for emp in out.employment:
        if not (emp.employer or emp.title):
            continue
        p.employment.append(
            EmploymentIn(
                employer=_clip(emp.employer, 200),
                title=_clip(emp.title, 200),
                location=_clip(emp.location, 200),
                start_date=parse_date(emp.start_date),
                end_date=parse_date(emp.end_date),
                is_current=bool(emp.is_current),
                description=(emp.description or None) and emp.description[:10000],
            )
        )
    for edu in out.education:
        if not (edu.institution or edu.degree):
            continue
        p.education.append(
            EducationIn(
                institution=_clip(edu.institution, 300),
                degree=_clip(edu.degree, 200),
                field=_clip(edu.field, 200),
                start_year=edu.start_year if edu.start_year and 1900 <= edu.start_year <= 2200 else None,
                end_year=edu.end_year if edu.end_year and 1900 <= edu.end_year <= 2200 else None,
                grade=_clip(edu.grade, 50),
            )
        )
    for cert in out.certifications:
        name = _clip(cert.name, 300)
        if not name:
            continue
        p.certifications.append(
            CertificationIn(
                name=name,
                issuer=_clip(cert.issuer, 200),
                issued_on=parse_date(cert.issued_on),
                expires_on=parse_date(cert.expires_on),
                credential_id=_clip(cert.credential_id, 200),
            )
        )
    return p


def prefill_of(p: Parsed) -> tuple[CandidatePrefill, list[str]]:
    """``CandidateCreate``-shaped prefill + the names of the fields that came from the parse."""
    skills_in: list[Any] = [
        CandidateSkillIn(
            name=ref.name, ref=SkillRefIn(kind=ref.kind, id=ref.id), years=y, last_used=lu, source="parsed"
        )
        for ref, y, lu in p.skills
    ]
    skills_in += [
        CandidateSkillIn(name=u.name, years=u.years, last_used=u.last_used, source="parsed")
        for u in p.unresolved
    ]
    prefill = CandidatePrefill(
        full_name=p.full_name,
        current_title=p.current_title,
        current_employer=p.current_employer,
        total_experience_months=p.total_experience_months,
        location=LocationIn(**p.location) if p.location else None,
        emails=[EmailIn(email=e, is_primary=i == 0) for i, e in enumerate(p.emails)],
        phones=[PhoneIn(phone=ph, is_primary=i == 0) for i, ph in enumerate(p.phones)],
        links=[LinkIn(url=u, kind=k) for u, k in p.links],
        employment=p.employment,
        education=p.education,
        certifications=p.certifications,
        skills=skills_in,
        source="resume_upload",
    )
    ai_fields = [
        f
        for f in (
            *SCALAR_DIFF_FIELDS,
            "emails",
            "phones",
            "links",
            "skills",
            "employment",
            "education",
            "certifications",
        )
        if getattr(prefill, f) not in (None, [], "")
    ]
    return prefill, ai_fields


# ================================================================ ingest
@dataclasses.dataclass
class Ingested:
    resume: CandidateResume
    parsed: Parsed | None
    ai_error: str | None


async def run_parse(
    ctx: RequestContext,
    db: AsyncSession,
    text: str,
    *,
    subject: tuple[str, uuid.UUID] | None,
    input_ref: str,
    bulk: bool = False,
) -> tuple[str, ResumeParseOutput | None, uuid.UUID | None, str | None]:
    """``(parse_status, output, ai_action_id, error_code)``; never raises."""
    result = await ai.run(
        ctx,
        db,
        FEATURE,
        PROMPT_ID,
        inputs={},
        untrusted={"document": text},
        output_schema=ResumeParseOutput,
        subject=subject,
        input_ref=input_ref,
        bulk=bulk,
    )
    if result.ok and isinstance(result.data, ResumeParseOutput):
        return "parsed", result.data, result.action_id, None
    return _AI_STATUS.get(result.error_code or "", "unavailable"), None, result.action_id, result.error_code


async def next_version(db: AsyncSession, candidate_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(CandidateResume.version), 0)).where(
        CandidateResume.candidate_id == candidate_id
    )
    return int((await db.execute(stmt)).scalar_one() or 0) + 1


async def ingest_file(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    file_id: uuid.UUID,
    data: bytes,
    candidate_id: uuid.UUID | None,
    input_ref_prefix: str = "resume",
    bulk: bool = False,
    resume_id: uuid.UUID | None = None,
) -> Ingested:
    """Create a resume row for an already stored file: extract text, parse, post-validate."""
    rid = resume_id or uuid.uuid4()
    kind = files_service.sniff_kind(data) or "unknown"
    extraction = extract_text(data, kind)
    resume = CandidateResume(
        id=rid,
        tenant_id=ctx.tenant_id,
        candidate_id=candidate_id,
        file_id=file_id,
        version=await next_version(db, candidate_id) if candidate_id else None,
        extracted_text=extraction.text,
        text_status=extraction.status,
        parse_status="failed_manual",
        uploaded_by=getattr(ctx.principal, "id", None),
    )
    db.add(resume)
    await db.flush()
    await files_service.set_file_owner(db, file_id, RESUME_FILE_OWNER, rid)
    parsed: Parsed | None = None
    error: str | None = extraction.status if extraction.status != "ok" else None
    if extraction.status == "ok" and extraction.text:
        status, output, action_id, error = await run_parse(
            ctx,
            db,
            extraction.text,
            subject=("candidate", candidate_id) if candidate_id else None,
            input_ref=f"{input_ref_prefix}:{rid}",
            bulk=bulk,
        )
        resume.parse_status = status
        resume.ai_action_id = action_id
        if output is not None:
            resume.parsed_data = output.model_dump(mode="json")
            parsed = await postprocess(ctx, db, output)
    await db.flush()
    logger.info(
        "Resume {} ingested: text={} parse={} error={}", rid, resume.text_status, resume.parse_status, error
    )
    return Ingested(resume, parsed, error)


async def _store_and_ingest(
    ctx: RequestContext, db: AsyncSession, data: bytes, filename: str | None, candidate_id: uuid.UUID | None
) -> Ingested:
    rid = uuid.uuid4()
    file = await files_service.store_upload(
        ctx, db, data, filename, files_service.RESUME_KINDS, RESUME_FILE_OWNER, rid
    )
    return await ingest_file(ctx, db, file_id=file.id, data=data, candidate_id=candidate_id, resume_id=rid)


async def parsed_of(ctx: RequestContext, db: AsyncSession, resume: CandidateResume) -> Parsed | None:
    if not resume.parsed_data:
        return None
    try:
        output = ResumeParseOutput.model_validate(resume.parsed_data)
    except ValueError:
        return None
    return await postprocess(ctx, db, output)


# ================================================================ create-from-resume
async def create_resume_draft(
    ctx: RequestContext, db: AsyncSession, data: bytes, filename: str | None
) -> ResumeDraftOut:
    ing = await _store_and_ingest(ctx, db, data, filename, None)
    warnings: list[str] = []
    possible = []
    if ing.parsed is not None:
        prefill, ai_fields = prefill_of(ing.parsed)
        warnings = list(ing.parsed.warnings)
        from app.services.candidates import duplicates

        probe = duplicates.Probe(
            full_name=ing.parsed.full_name,
            current_employer=ing.parsed.current_employer,
            city=(ing.parsed.location or {}).get("city"),
            emails=tuple(ing.parsed.emails),
            phones=tuple(ing.parsed.phones),
            links=tuple(u for u, _ in ing.parsed.links),
        )
        matches = await duplicates.probe_matches(db, probe)
        possible = await duplicates.visible_summaries(ctx, db, list(matches))
        unresolved = ing.parsed.unresolved
    else:
        prefill, ai_fields, unresolved = CandidatePrefill(source="resume_upload"), [], []
    return ResumeDraftOut(
        resume=await resume_out(db, ing.resume),
        parse_status=ing.resume.parse_status,
        prefill=prefill,
        ai_fields=ai_fields,
        unresolved_skills=unresolved,
        warnings=warnings,
        possible_duplicates=possible,
    )


async def claim_draft(ctx: RequestContext, db: AsyncSession, resume_id: uuid.UUID) -> CandidateResume:
    """Lock and return a draft resume (no candidate yet) uploaded by the caller."""
    stmt = (
        select(CandidateResume)
        .where(
            CandidateResume.id == resume_id,
            CandidateResume.candidate_id.is_(None),
            CandidateResume.uploaded_by == principal_of(ctx).id,
        )
        .with_for_update()
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Resume draft not found", code="resume_not_found")
    return row


def _key_set(field: str, value: Any) -> set[str]:
    if not value:
        return set()
    if field == "emails":
        return {(normalize_email(e.email) or e.email).lower() for e in value}
    if field == "phones":
        return {normalize_phone(p.phone) or p.phone for p in value}
    if field == "links":
        return {normalize_url(lk.url) or lk.url for lk in value}
    if field == "skills":
        return {
            (s if isinstance(s, str) else (s.name or str(s.ref.id if s.ref else ""))).lower() for s in value
        }
    if field == "employment":
        return {f"{(e.employer or '').lower()}|{(e.title or '').lower()}" for e in value}
    if field == "education":
        return {f"{(e.institution or '').lower()}|{(e.degree or '').lower()}" for e in value}
    if field == "certifications":
        return {c.name.lower() for c in value}
    return set()


def decision_for_create(prefill: CandidatePrefill, submitted: CandidateCreate) -> str | None:
    """Classify the human decision: accepted = all parsed values used unchanged, rejected = none used,
    edited = otherwise.

    A scalar replaced by a different value, or a list without any parsed entry, counts as "not used".
    """
    same = partial = unused = 0
    for field in SCALAR_DIFF_FIELDS:
        parsed_value = getattr(prefill, field)
        if parsed_value in (None, ""):
            continue
        if getattr(submitted, field) == parsed_value:
            same += 1
        else:
            unused += 1
    for field in ("emails", "phones", "links", "skills", "employment", "education", "certifications"):
        theirs = _key_set(field, getattr(prefill, field))
        if not theirs:
            continue
        mine = _key_set(field, getattr(submitted, field))
        if mine == theirs:
            same += 1
        elif mine & theirs:
            partial += 1
        else:
            unused += 1
    if same + partial + unused == 0:
        return None
    if partial == 0 and unused == 0:
        return "accepted"
    if same == 0 and partial == 0:
        return "rejected"
    return "edited"


async def attach_draft(
    ctx: RequestContext, db: AsyncSession, draft: CandidateResume, cand: Candidate, submitted: CandidateCreate
) -> None:
    draft.candidate_id = cand.id
    draft.version = await next_version(db, cand.id)
    await db.flush()
    if draft.ai_action_id is not None:
        await ai.set_subject(db, draft.ai_action_id, "candidate", cand.id)
        parsed = await parsed_of(ctx, db, draft)
        if parsed is not None and draft.parse_status == "parsed":
            prefill, _ = prefill_of(parsed)
            decision = decision_for_create(prefill, submitted)
            if decision is not None:
                await ai.record_decision(ctx, db, draft.ai_action_id, decision)
    await activity_port.add_system_entry(
        ctx, db, "candidate", cand.id, "Resume uploaded", resume_id=str(draft.id), version=draft.version
    )


# ================================================================ existing candidate: upload + diff + apply
async def _current(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    async def _col(col: Any, fk: Any) -> list[Any]:
        return list((await db.execute(select(col).where(fk == candidate_id))).scalars())

    emp = list(
        (
            await db.execute(
                select(CandidateEmployment).where(CandidateEmployment.candidate_id == candidate_id)
            )
        ).scalars()
    )
    edu = list(
        (
            await db.execute(
                select(CandidateEducation).where(CandidateEducation.candidate_id == candidate_id)
            )
        ).scalars()
    )
    skills_rows = list(
        (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    )
    return {
        "emails": set(await _col(CandidateEmail.email, CandidateEmail.candidate_id)),
        "phones": set(await _col(CandidatePhone.phone_e164, CandidatePhone.candidate_id)),
        "links": set(await _col(CandidateLink.url_norm, CandidateLink.candidate_id)),
        "certifications": {
            n.lower() for n in await _col(CandidateCertification.name, CandidateCertification.candidate_id)
        },
        "employment": {f"{(e.employer or '').lower()}|{(e.title or '').lower()}" for e in emp},
        "education": {f"{(e.institution or '').lower()}|{(e.degree or '').lower()}" for e in edu},
        "skills": {
            ("global", s.skill_id) if s.skill_id else ("tenant", s.tenant_skill_id) for s in skills_rows
        },
    }


def _loc(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    loc = {k: value.get(k) for k in ("city", "state", "country")}
    return loc if any(loc.values()) else None


async def compute_diff(db: AsyncSession, cand: Candidate, p: Parsed) -> list[DiffEntry]:
    diff: list[DiffEntry] = []
    for field in SCALAR_DIFF_FIELDS:
        parsed_value: Any = getattr(p, field)
        current: Any = getattr(cand, field)
        if field == "location":
            parsed_value, current = _loc(parsed_value), _loc(current)
        if parsed_value in (None, "") or parsed_value == current:
            continue
        diff.append(DiffEntry(field=field, current=current, parsed=parsed_value))
    cur = await _current(db, cand.id)
    diff += [DiffEntry(field="emails", parsed=e) for e in p.emails if e not in cur["emails"]]
    diff += [DiffEntry(field="phones", parsed=ph) for ph in p.phones if ph not in cur["phones"]]
    diff += [DiffEntry(field="links", parsed=u) for u, _ in p.links if u not in cur["links"]]
    diff += [
        DiffEntry(field="skills", parsed=ref.name)
        for ref, _, _ in p.skills
        if (ref.kind, ref.id) not in cur["skills"]
    ]
    diff += [DiffEntry(field="skills", parsed=u.name) for u in p.unresolved]
    for i, emp in enumerate(p.employment):
        if f"{(emp.employer or '').lower()}|{(emp.title or '').lower()}" not in cur["employment"]:
            diff.append(DiffEntry(field="employment", index=i, parsed=emp.model_dump(mode="json")))
    for i, edu in enumerate(p.education):
        if f"{(edu.institution or '').lower()}|{(edu.degree or '').lower()}" not in cur["education"]:
            diff.append(DiffEntry(field="education", index=i, parsed=edu.model_dump(mode="json")))
    for i, cert in enumerate(p.certifications):
        if cert.name.lower() not in cur["certifications"]:
            diff.append(DiffEntry(field="certifications", index=i, parsed=cert.model_dump(mode="json")))
    return diff


async def upload_resume(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: bytes, filename: str | None
) -> ResumeUploadOut:
    cand = await get_editable(ctx, db, candidate_id, for_update=True)
    ing = await _store_and_ingest(ctx, db, data, filename, cand.id)
    diff: list[DiffEntry] = []
    warnings: list[str] = []
    unresolved: list[UnresolvedSkill] = []
    if ing.parsed is not None:
        diff = await compute_diff(db, cand, ing.parsed)
        warnings = list(ing.parsed.warnings)
        unresolved = ing.parsed.unresolved
    await activity_port.add_system_entry(
        ctx,
        db,
        "candidate",
        cand.id,
        "Resume uploaded",
        resume_id=str(ing.resume.id),
        version=ing.resume.version,
    )
    return ResumeUploadOut(
        resume=await resume_out(db, ing.resume),
        parse_status=ing.resume.parse_status,
        diff=diff,
        unresolved_skills=unresolved,
        warnings=warnings,
    )


async def _resume_of(db: AsyncSession, candidate_id: uuid.UUID, resume_id: uuid.UUID) -> CandidateResume:
    row = await db.get(CandidateResume, resume_id)
    if row is None or row.candidate_id != candidate_id:
        raise NotFound("Resume not found")
    return row


async def apply_resume(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, resume_id: uuid.UUID, body: ResumeApply
) -> dict[str, Any]:
    cand = await get_editable(ctx, db, candidate_id, for_update=True)
    resume = await _resume_of(db, cand.id, resume_id)
    parsed = await parsed_of(ctx, db, resume)
    if parsed is None:
        from app.core.errors import ConflictError

        raise ConflictError("This resume has no parsed data to apply", code="resume_not_parsed")
    diff = await compute_diff(db, cand, parsed)
    applied = 0
    tid = ctx.tenant_id

    def _selected(entry: DiffEntry) -> bool:
        if entry.field in SCALAR_DIFF_FIELDS:
            return entry.field in body.fields
        if entry.field in {"emails", "phones", "links", "skills"}:
            return str(entry.parsed) in set(getattr(body, entry.field))
        idx = {
            "employment": body.employment_idx,
            "education": body.education_idx,
            "certifications": body.certification_idx,
        }
        return entry.index in set(idx[entry.field])

    skills_to_add: list[Any] = []
    for entry in diff:
        if not _selected(entry):
            continue
        applied += 1
        if entry.field in SCALAR_DIFF_FIELDS:
            setattr(cand, entry.field, entry.parsed)
        elif entry.field == "emails":
            db.add(CandidateEmail(tenant_id=tid, candidate_id=cand.id, email=entry.parsed, is_primary=False))
        elif entry.field == "phones":
            db.add(
                CandidatePhone(tenant_id=tid, candidate_id=cand.id, phone_e164=entry.parsed, is_primary=False)
            )
        elif entry.field == "links":
            db.add(
                CandidateLink(
                    tenant_id=tid, candidate_id=cand.id, url_norm=entry.parsed, kind=link_kind(entry.parsed)
                )
            )
        elif entry.field == "skills":
            match = next((s for s in parsed.skills if s[0].name == entry.parsed), None)
            if match is not None:
                ref, years, last_used = match
                skills_to_add.append(
                    CandidateSkillIn(
                        ref=SkillRefIn(kind=ref.kind, id=ref.id),
                        years=years,
                        last_used=last_used,
                        source="parsed",
                    )
                )
            else:
                u = next((u for u in parsed.unresolved if u.name == entry.parsed), None)
                if u is not None:
                    skills_to_add.append(
                        CandidateSkillIn(name=u.name, years=u.years, last_used=u.last_used, source="parsed")
                    )
        elif entry.field == "employment" and entry.index is not None:
            db.add(records.add_employment(tid, cand.id, parsed.employment[entry.index]))
        elif entry.field == "education" and entry.index is not None:
            db.add(records.add_education(tid, cand.id, parsed.education[entry.index]))
        elif entry.field == "certifications" and entry.index is not None:
            db.add(records.add_certification(tid, cand.id, parsed.certifications[entry.index]))
    await db.flush()
    if skills_to_add:
        await records.replace_skills(ctx, db, cand.id, skills_to_add, created_from="parse", replace=False)
    decision: str | None = None
    if diff:
        decision = "accepted" if applied == len(diff) else "rejected" if applied == 0 else "edited"
        if resume.ai_action_id is not None:
            await ai.record_decision(ctx, db, resume.ai_action_id, decision)
    from app.services.candidates import duplicates

    await duplicates.find_duplicates(ctx, db, cand)
    return {"candidate": await records.build_360(ctx, db, cand), "decision": decision}


async def list_resumes(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[ResumeOut]:
    await get_visible(ctx, db, candidate_id)
    return await records.resume_outs(db, candidate_id)


async def list_ai_actions(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[Any]:
    await get_visible(ctx, db, candidate_id)
    return await ai.list_actions(ctx, db, "candidate", candidate_id)
