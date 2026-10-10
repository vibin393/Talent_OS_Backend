"""Candidate records (FR-18, FR-22, FR-24; story 3.1): create / edit / list / Candidate 360 / archive /
owner change / skills."""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.pagination import Page, PageParams
from app.core.permissions import (
    Principal,
    can_view_compensation,
    forbidden,
    is_compensation_field,
    mask_compensation,
)
from app.core.sequences import next_human_id
from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateDuplicate,
    CandidateEducation,
    CandidateEmail,
    CandidateEmployment,
    CandidateLink,
    CandidateList,
    CandidateListMember,
    CandidatePhone,
    CandidateResume,
    CandidateSkill,
    CandidateTag,
    Tag,
)
from app.schemas.candidates import (
    Candidate360Out,
    CandidateCounts,
    CandidateCreate,
    CandidateFilters,
    CandidateOut,
    CandidateSkillIn,
    CandidateSkillOut,
    CandidateSkillsSet,
    CandidateUpdate,
    CertificationIn,
    CertificationOut,
    EducationIn,
    EducationOut,
    EmailIn,
    EmailOut,
    EmploymentIn,
    EmploymentOut,
    LinkIn,
    LinkOut,
    ListRefOut,
    LocationOut,
    PhoneIn,
    PhoneOut,
    SkillInput,
    SkillRef,
    TagOut,
)
from app.services import files_service
from app.services.candidates import activity_port, skills
from app.services.candidates.common import (
    CREATE,
    DELETE,
    READ,
    UPDATE,
    active_user_exists,
    can_access,
    field_error,
    get_editable,
    get_for,
    get_visible,
    link_kind,
    normalize_email,
    normalize_phone,
    normalize_url,
    now,
    principal_of,
    read_condition,
    user_names,
)

SCALAR_FIELDS: tuple[str, ...] = (
    "full_name",
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
    "source",
    "source_detail",
    "owner_id",
)
CHILD_LISTS = ("emails", "phones", "links", "employment", "education", "certifications", "skills")

PARSE_LABELS = {
    "parsed": "Parsed",
    "failed_manual": "Parse failed – manual entry",  # noqa: RUF001
    "unavailable": "AI unavailable – fill manually",  # noqa: RUF001
    "skipped_disabled": "AI parsing disabled",
    "skipped_consent": "AI parsing not permitted (consent)",
}


# ================================================================ normalisation of nested input
def _jsonable_scalar(field: str, value: Any) -> Any:
    if field == "location":
        return value.model_dump() if value is not None else None
    if field == "preferred_locations":
        return [v.model_dump() for v in value] if value is not None else None
    return value


def _emails(items: Sequence[EmailIn]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        email = normalize_email(item.email)
        if email is None:
            raise field_error(f"emails.{i}.email", "Invalid email address")
        if email in seen:
            continue
        seen.add(email)
        out.append((email, item.is_primary))
    return _one_primary(out)


def _phones(items: Sequence[PhoneIn]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        phone = normalize_phone(item.phone)
        if phone is None:
            raise field_error(f"phones.{i}.phone", "Invalid phone number", code="invalid_phone")
        if phone in seen:
            continue
        seen.add(phone)
        out.append((phone, item.is_primary))
    return _one_primary(out)


def _one_primary(rows: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    if not rows:
        return rows
    first = next((i for i, (_, p) in enumerate(rows) if p), 0)
    return [(v, i == first) for i, (v, _) in enumerate(rows)]


def _links(items: Sequence[LinkIn]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        url = normalize_url(item.url)
        if url is None:
            raise field_error(f"links.{i}.url", "Invalid URL")
        if url in seen:
            continue
        seen.add(url)
        out.append((url, item.kind or link_kind(url)))
    return out


async def _replace_children(
    ctx: RequestContext, db: AsyncSession, cand: Candidate, data: CandidateCreate | CandidateUpdate
) -> None:
    """Write every provided child list (replacing existing rows)."""
    tid = ctx.tenant_id

    async def _clear(model: Any) -> None:
        for row in (await db.execute(select(model).where(model.candidate_id == cand.id))).scalars():
            await db.delete(row)
        await db.flush()

    if data.emails is not None:
        emails = _emails(data.emails)
        await _clear(CandidateEmail)
        for email, primary in emails:
            db.add(CandidateEmail(tenant_id=tid, candidate_id=cand.id, email=email, is_primary=primary))
    if data.phones is not None:
        phones = _phones(data.phones)
        await _clear(CandidatePhone)
        for phone, primary in phones:
            db.add(CandidatePhone(tenant_id=tid, candidate_id=cand.id, phone_e164=phone, is_primary=primary))
    if data.links is not None:
        links = _links(data.links)
        await _clear(CandidateLink)
        for url, kind in links:
            db.add(CandidateLink(tenant_id=tid, candidate_id=cand.id, url_norm=url, kind=kind))
    if data.employment is not None:
        await _clear(CandidateEmployment)
        for emp in data.employment:
            db.add(add_employment(tid, cand.id, emp))
    if data.education is not None:
        await _clear(CandidateEducation)
        for edu in data.education:
            db.add(add_education(tid, cand.id, edu))
    if data.certifications is not None:
        await _clear(CandidateCertification)
        for cert in data.certifications:
            db.add(add_certification(tid, cand.id, cert))
    await db.flush()


def add_employment(
    tenant_id: uuid.UUID | None, candidate_id: uuid.UUID, emp: EmploymentIn
) -> CandidateEmployment:
    return CandidateEmployment(tenant_id=tenant_id, candidate_id=candidate_id, **emp.model_dump())


def add_education(
    tenant_id: uuid.UUID | None, candidate_id: uuid.UUID, edu: EducationIn
) -> CandidateEducation:
    return CandidateEducation(tenant_id=tenant_id, candidate_id=candidate_id, **edu.model_dump())


def add_certification(
    tenant_id: uuid.UUID | None, candidate_id: uuid.UUID, cert: CertificationIn
) -> CandidateCertification:
    return CandidateCertification(tenant_id=tenant_id, candidate_id=candidate_id, **cert.model_dump())


# ================================================================ skills
async def _resolve_input(
    ctx: RequestContext, db: AsyncSession, item: SkillInput, index: int, created_from: str
) -> tuple[SkillRef | None, CandidateSkillIn]:
    spec = CandidateSkillIn(name=item) if isinstance(item, str) else item
    if spec.ref is not None:
        ref = await skills.get_ref(db, spec.ref)
        if ref is None:
            raise field_error(f"skills.{index}.ref", "Unknown skill", code="invalid_reference")
        return ref, spec
    if not spec.name:
        raise field_error(f"skills.{index}", "A skill needs a name or a ref")
    return await skills.resolve_skill(
        ctx, db, spec.name, create_pending=True, created_from=created_from
    ), spec


async def replace_skills(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    items: Sequence[SkillInput],
    *,
    created_from: str = "manual",
    replace: bool = True,
) -> list[str]:
    """Set (or, with ``replace=False``, add to) a candidate's skills. Returns warnings (skipped skills)."""
    warnings: list[str] = []
    wanted: dict[tuple[str, Any], CandidateSkillIn] = {}
    for i, item in enumerate(items):
        ref, spec = await _resolve_input(ctx, db, item, i, created_from)
        if ref is None or ref.status == "rejected":
            warnings.append(f"skill_rejected:{i}")
            continue
        key = (ref.kind, ref.id)
        prev = wanted.get(key)
        if prev is None or (spec.years or Decimal(0)) > (prev.years or Decimal(0)):
            wanted[key] = spec
    existing: dict[tuple[str, Any], CandidateSkill] = {
        (("global", r.skill_id) if r.skill_id else ("tenant", r.tenant_skill_id)): r
        for r in (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    }
    if replace:
        for ekey, stale in existing.items():
            if ekey not in wanted:
                await db.delete(stale)
    for (kind, sid), spec in wanted.items():
        row = existing.get((kind, sid))
        if row is None:
            db.add(
                CandidateSkill(
                    tenant_id=ctx.tenant_id,
                    candidate_id=candidate_id,
                    skill_id=sid if kind == "global" else None,
                    tenant_skill_id=sid if kind == "tenant" else None,
                    source=spec.source,
                    years=spec.years,
                    last_used=spec.last_used,
                )
            )
        else:
            if replace or spec.years is not None:
                row.years = spec.years if replace else max(spec.years or Decimal(0), row.years or Decimal(0))
            if replace or spec.last_used is not None:
                row.last_used = spec.last_used
            if replace:
                row.source = spec.source
    await db.flush()
    return warnings


async def skills_out(db: AsyncSession, candidate_id: uuid.UUID) -> list[CandidateSkillOut]:
    rows = list(
        (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        ).scalars()
    )
    gmap, tmap = await skills.refs_for(
        db, [r.skill_id for r in rows if r.skill_id], [r.tenant_skill_id for r in rows if r.tenant_skill_id]
    )
    out: list[CandidateSkillOut] = []
    for r in rows:
        ref = gmap.get(r.skill_id) if r.skill_id else tmap.get(r.tenant_skill_id)  # type: ignore[arg-type]
        if ref is None:
            continue
        out.append(
            CandidateSkillOut(
                id=r.id,
                skill=ref,
                source=r.source,
                years=r.years,
                last_used=r.last_used,
                proficiency=r.proficiency,
                verification=r.verification,
            )
        )
    out.sort(key=lambda s: (-(s.years or Decimal(0)), s.skill.name.lower()))
    return out


# ================================================================ create / update
async def _check_owner(db: AsyncSession, owner_id: uuid.UUID) -> None:
    if not await active_user_exists(db, owner_id):
        raise ValidationFailed(
            "Unknown or inactive owner",
            code="invalid_reference",
            details={"fields": [{"field": "owner_id", "message": "Unknown or inactive user"}]},
        )


async def create_record(
    ctx: RequestContext,
    db: AsyncSession,
    data: CandidateCreate,
    *,
    owner_id: uuid.UUID,
    source: str,
    skill_origin: str = "manual",
) -> tuple[Candidate, list[str]]:
    """Insert a candidate with children, skills and tags (no permission checks; callers do them)."""
    # Validate children before allocating a human id (a 422 must not burn a CAN number... it would roll back
    # anyway, but keep the order obvious).
    _emails(data.emails)
    _phones(data.phones)
    _links(data.links)
    await _check_owner(db, owner_id)
    from app.services import privacy, settings_service

    cand = Candidate(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        # Epic 12 (FR-74): tenant-configurable prefix; the sequence key stays "CAN" so numbering continues.
        human_id=await next_human_id(db, "CAN", prefix=await settings_service.id_prefix(db, "CAN"), width=6),
        source=source,
        owner_id=owner_id,
        **{
            f: _jsonable_scalar(f, getattr(data, f)) for f in SCALAR_FIELDS if f not in {"source", "owner_id"}
        },
    )
    # Epic 12 (FR-67): the privacy service decides the initial "AI consent" flag (basis + source).
    cand.ai_consent_state = await privacy.initial_ai_consent_state(db, source)
    db.add(cand)
    await db.flush()
    await _replace_children(ctx, db, cand, data)
    warnings = await replace_skills(ctx, db, cand.id, data.skills, created_from=skill_origin)
    if data.tags:
        from app.services.candidates import tags_lists

        await tags_lists.attach_tags(ctx, db, cand.id, data.tags)
    await activity_port.add_system_entry(ctx, db, "candidate", cand.id, "Candidate created", source=source)
    logger.info("Candidate {} created ({})", cand.id, cand.human_id)
    return cand, warnings


async def create_candidate(ctx: RequestContext, db: AsyncSession, data: CandidateCreate) -> dict[str, Any]:
    principal = principal_of(ctx)
    owner_id = data.owner_id or principal.id
    source = data.source or ("resume_upload" if data.resume_id else "manual")
    draft = None
    if data.resume_id is not None:
        from app.services.candidates import parsing

        draft = await parsing.claim_draft(ctx, db, data.resume_id)
    cand, warnings = await create_record(ctx, db, data, owner_id=owner_id, source=source)
    if draft is not None:
        from app.services.candidates import parsing

        await parsing.attach_draft(ctx, db, draft, cand, data)
    from app.services.candidates import duplicates

    dups = await duplicates.find_duplicates(ctx, db, cand)
    out = await build_360(ctx, db, cand)
    possible = await duplicates.visible_summaries(ctx, db, [d.other_id for d in dups])
    return {**out, "warnings": {"possible_duplicates": possible, "skills": warnings}}


async def update_candidate(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: CandidateUpdate
) -> dict[str, Any]:
    cand = await get_editable(ctx, db, candidate_id, for_update=True)
    provided = data.model_fields_set
    for field in SCALAR_FIELDS:
        if field not in provided:
            continue
        value = getattr(data, field)
        if field in {"full_name", "source", "owner_id"} and value is None:
            raise field_error(field, "This field cannot be empty")
        if field == "owner_id":
            continue
        setattr(cand, field, _jsonable_scalar(field, value))
    if "owner_id" in provided and data.owner_id != cand.owner_id:
        await _change_owner(ctx, db, cand, data.owner_id)  # type: ignore[arg-type]
    await _replace_children(ctx, db, cand, data)
    if data.skills is not None:
        await replace_skills(ctx, db, cand.id, data.skills)
    await db.flush()
    from app.services.candidates import duplicates

    await duplicates.find_duplicates(ctx, db, cand)
    return await build_360(ctx, db, cand)


async def _change_owner(ctx: RequestContext, db: AsyncSession, cand: Candidate, owner_id: uuid.UUID) -> None:
    await _check_owner(db, owner_id)
    old = cand.owner_id
    cand.owner_id = owner_id
    await db.flush()
    audit.record(
        "candidate.owner_changed", "candidate", cand.id, session=db, old_owner_id=old, new_owner_id=owner_id
    )
    await activity_port.add_system_entry(
        ctx, db, "candidate", cand.id, "Owner changed", old_owner_id=str(old), new_owner_id=str(owner_id)
    )


async def set_skills(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: CandidateSkillsSet
) -> list[CandidateSkillOut]:
    await get_editable(ctx, db, candidate_id)
    await replace_skills(ctx, db, candidate_id, data.skills)
    return await skills_out(db, candidate_id)


async def archive_candidate(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    cand = await get_for(ctx, db, candidate_id, DELETE, for_update=True)
    if cand.archived_at is None:
        cand.archived_at = now()
        await db.flush()
    return await build_360(ctx, db, cand)


async def unarchive_candidate(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID
) -> dict[str, Any]:
    cand = await get_for(ctx, db, candidate_id, DELETE, for_update=True)
    if cand.merged_into_id is not None:
        from app.core.errors import ConflictError

        raise ConflictError(
            "A merged candidate cannot be unarchived; undo the merge", code="candidate_merged"
        )
    if cand.archived_at is not None:
        cand.archived_at = None
        await db.flush()
    return await build_360(ctx, db, cand)


# ================================================================ read
def _location(value: Any) -> LocationOut | None:
    return LocationOut(**value) if isinstance(value, dict) else None


async def _primary(
    db: AsyncSession, ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    emails: dict[uuid.UUID, str] = {}
    phones: dict[uuid.UUID, str] = {}
    if not ids:
        return emails, phones
    for cid, email in await db.execute(
        select(CandidateEmail.candidate_id, CandidateEmail.email)
        .where(CandidateEmail.candidate_id.in_(ids))
        .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
    ):
        emails.setdefault(cid, email)
    for cid, phone in await db.execute(
        select(CandidatePhone.candidate_id, CandidatePhone.phone_e164)
        .where(CandidatePhone.candidate_id.in_(ids))
        .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
    ):
        phones.setdefault(cid, phone)
    return emails, phones


async def summaries(db: AsyncSession, rows: Sequence[Candidate]) -> list[CandidateOut]:
    ids = [r.id for r in rows]
    emails, phones = await _primary(db, ids)
    names = await user_names(db, {r.owner_id for r in rows})
    return [
        CandidateOut(
            id=r.id,
            human_id=r.human_id,
            full_name=r.full_name,
            current_title=r.current_title,
            current_employer=r.current_employer,
            location=_location(r.location),
            total_experience_months=r.total_experience_months,
            source=r.source,
            owner_id=r.owner_id,
            owner_name=names.get(r.owner_id),
            primary_email=emails.get(r.id),
            primary_phone=phones.get(r.id),
            archived_at=r.archived_at,
            merged_into_id=r.merged_into_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
            ai_consent_state=r.ai_consent_state,  # type: ignore[arg-type]
        )
        for r in rows
    ]


def _like(term: str) -> str:
    escaped = term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_SORTS: dict[str, tuple[Any, ...]] = {
    "name": (Candidate.full_name.asc(),),
    "-name": (Candidate.full_name.desc(),),
    "created_at": (Candidate.created_at.asc(),),
    "-created_at": (Candidate.created_at.desc(),),
    "updated_at": (Candidate.updated_at.desc(),),
    "-updated_at": (Candidate.updated_at.desc(),),
}


def list_stmt(principal: Principal, filters: CandidateFilters) -> Select[tuple[Candidate]]:
    stmt = select(Candidate).where(Candidate.merged_into_id.is_(None))
    cond = read_condition(principal)
    if cond is not None:
        stmt = stmt.where(cond)
    if not filters.include_archived:
        stmt = stmt.where(Candidate.archived_at.is_(None))
    if filters.q and filters.q.strip():
        pattern = _like(filters.q)
        email_match = exists().where(
            CandidateEmail.candidate_id == Candidate.id, CandidateEmail.email.ilike(pattern, escape="\\")
        )
        stmt = stmt.where(
            or_(
                Candidate.full_name.ilike(pattern, escape="\\"),
                Candidate.human_id.ilike(pattern, escape="\\"),
                Candidate.current_title.ilike(pattern, escape="\\"),
                Candidate.current_employer.ilike(pattern, escape="\\"),
                email_match,
            )
        )
    if filters.owner_id is not None:
        stmt = stmt.where(Candidate.owner_id == filters.owner_id)
    if filters.source is not None:
        stmt = stmt.where(Candidate.source == filters.source)
    if filters.ai_consent_state is not None:
        stmt = stmt.where(Candidate.ai_consent_state == filters.ai_consent_state)
    if filters.tag_id is not None:
        stmt = stmt.where(
            exists().where(CandidateTag.candidate_id == Candidate.id, CandidateTag.tag_id == filters.tag_id)
        )
    if filters.list_id is not None:
        from app.services.candidates import tags_lists

        stmt = stmt.where(
            exists().where(
                CandidateListMember.candidate_id == Candidate.id,
                CandidateListMember.list_id == filters.list_id,
                CandidateListMember.list_id.in_(tags_lists.visible_list_ids(principal)),
            )
        )
    return stmt.order_by(*_SORTS[filters.sort], Candidate.id)


def list_query(ctx: RequestContext, filters: CandidateFilters) -> Select[tuple[Candidate]]:
    """Return the scoped, filtered and ordered candidate list query (Epic 11: the single source of
    ``GET /candidates`` and the candidates export)."""
    return list_stmt(principal_of(ctx), filters)


async def list_candidates(
    ctx: RequestContext, db: AsyncSession, filters: CandidateFilters, params: PageParams
) -> Page[CandidateOut]:
    stmt = list_query(ctx, filters)
    total = int(
        (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    )
    rows = list((await db.execute(stmt.limit(params.page_size).offset(params.offset))).scalars())
    return Page[CandidateOut](
        items=await summaries(db, rows), total=total, page=params.page, page_size=params.page_size
    )


async def resume_outs(db: AsyncSession, candidate_id: uuid.UUID) -> list[Any]:
    from app.services.candidates import parsing

    rows = (
        await db.execute(
            select(CandidateResume)
            .where(CandidateResume.candidate_id == candidate_id)
            .order_by(CandidateResume.version.desc())
        )
    ).scalars()
    return [await parsing.resume_out(db, r) for r in rows]


async def build_360(ctx: RequestContext, db: AsyncSession, cand: Candidate) -> dict[str, Any]:
    """Candidate 360 as a JSON-ready dict with compensation masked for the caller."""
    principal = principal_of(ctx)
    cid = cand.id
    await db.refresh(cand)

    async def _rows(model: Any, *order: Any) -> list[Any]:
        return list(
            (await db.execute(select(model).where(model.candidate_id == cid).order_by(*order))).scalars()
        )

    emails = await _rows(CandidateEmail, CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
    phones = await _rows(CandidatePhone, CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
    links = await _rows(CandidateLink, CandidateLink.created_at)
    employment = await _rows(
        CandidateEmployment,
        CandidateEmployment.is_current.desc(),
        CandidateEmployment.start_date.desc().nulls_last(),
        CandidateEmployment.created_at,
    )
    education = await _rows(
        CandidateEducation, CandidateEducation.end_year.desc().nulls_last(), CandidateEducation.created_at
    )
    certs = await _rows(CandidateCertification, CandidateCertification.created_at)
    skill_list = await skills_out(db, cid)
    tags = list(
        (
            await db.execute(
                select(Tag)
                .join(CandidateTag, CandidateTag.tag_id == Tag.id)
                .where(CandidateTag.candidate_id == cid)
                .order_by(Tag.name)
            )
        ).scalars()
    )
    from app.services.candidates import tags_lists

    lists = list(
        (
            await db.execute(
                select(CandidateList)
                .join(CandidateListMember, CandidateListMember.list_id == CandidateList.id)
                .where(
                    CandidateListMember.candidate_id == cid,
                    CandidateList.archived_at.is_(None),
                    CandidateList.id.in_(tags_lists.visible_list_ids(principal)),
                )
                .order_by(CandidateList.name)
            )
        ).scalars()
    )
    resumes = await resume_outs(db, cid)
    open_dups = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CandidateDuplicate)
                .where(
                    CandidateDuplicate.status == "open",
                    or_(CandidateDuplicate.candidate_a_id == cid, CandidateDuplicate.candidate_b_id == cid),
                )
            )
        ).scalar_one()
    )
    (summary,) = await summaries(db, [cand])
    out = Candidate360Out(
        **summary.model_dump(),
        current_ctc_amount=cand.current_ctc_amount,
        current_ctc_currency=cand.current_ctc_currency,
        current_ctc_unit=cand.current_ctc_unit,
        expected_ctc_amount=cand.expected_ctc_amount,
        expected_ctc_currency=cand.expected_ctc_currency,
        expected_ctc_unit=cand.expected_ctc_unit,
        availability_date=cand.availability_date,
        notice_period_days=cand.notice_period_days,
        last_working_date=cand.last_working_date,
        work_authorization=cand.work_authorization,
        willing_to_relocate=cand.willing_to_relocate,
        preferred_locations=[LocationOut(**p) for p in cand.preferred_locations or [] if isinstance(p, dict)]
        or None,
        source_detail=cand.source_detail,
        emails=[EmailOut(id=e.id, email=e.email, is_primary=e.is_primary) for e in emails],
        phones=[PhoneOut(id=p.id, phone_e164=p.phone_e164, is_primary=p.is_primary) for p in phones],
        links=[LinkOut(id=lk.id, url_norm=lk.url_norm, kind=lk.kind) for lk in links],
        employment=[EmploymentOut.model_validate(e, from_attributes=True) for e in employment],
        education=[EducationOut.model_validate(e, from_attributes=True) for e in education],
        certifications=[CertificationOut.model_validate(c, from_attributes=True) for c in certs],
        skills=skill_list,
        tags=[TagOut(id=t.id, name=t.name, color=t.color) for t in tags],
        lists=[ListRefOut(id=lst.id, name=lst.name) for lst in lists],
        resumes=resumes,
        counts=CandidateCounts(
            resumes=len(resumes),
            attachments=await files_service.count_attachments(db, "candidate", cid),
            skills=len(skill_list),
            tags=len(tags),
            lists=len(lists),
            open_duplicates=open_dups,
        ),
        applications=await _applications(ctx, db, cid),
        can_edit=await can_access(db, principal, cid, UPDATE),
    )
    return mask(out, principal)


async def _applications(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[Any]:
    """Epic 6: the caller's visible applications of this candidate (lazy import; empty without the pipeline)."""
    principal = ctx.principal
    if not isinstance(principal, Principal) or "application:read" not in principal.permissions:
        return []
    from app.services import pipeline as pipeline_service

    rows = await pipeline_service.applications.candidate_applications(ctx, db, candidate_id)
    return [r.model_dump(mode="json") for r in rows]


def mask(out: Candidate360Out, principal: Principal) -> dict[str, Any]:
    """Mask compensation (``*_ctc_*``) via ``mask_compensation`` unless the caller may view it."""
    data: dict[str, Any] = out.model_dump(mode="json")
    if can_view_compensation(principal):
        return data
    masked: dict[str, Any] = mask_compensation(data, principal)
    masked["masked_fields"] = sorted(k for k in data if is_compensation_field(k))
    return masked


async def get_candidate_360(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    cand = await get_visible(ctx, db, candidate_id)
    return await build_360(ctx, db, cand)


async def ensure_readable(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> Candidate:
    return await get_visible(ctx, db, candidate_id)


def require_create(ctx: RequestContext) -> None:
    if not principal_of(ctx).has(CREATE):
        raise forbidden(CREATE)


__all__ = [
    "READ",
    "build_360",
    "create_candidate",
    "get_candidate_360",
    "list_candidates",
    "update_candidate",
]
