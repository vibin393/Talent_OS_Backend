# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Campus registrations (Epic 43 story 43.2, FR-218): the status machine, reads, withdraw and the public
registration page (Secure Link purpose ``campus_registration``, AD-53).

States (PRD 5.9; every other transition 409 ``transition_not_allowed``): ``registered -> eligible | not_eligible``
(system); re-evaluation flips ``eligible <-> not_eligible`` (system); ``not_eligible -> eligible`` by override (star,
human); ``eligible -> assessed`` (system, scored invitation); ``assessed -> shortlisted`` (star, human);
``shortlisted -> interviewed`` (system, interview outcome); ``interviewed -> offered``; ``offered -> joined | reneged |
rejected``; any non-terminal ``-> withdrawn``. Each state sets its milestone timestamp (the funnel counts them).

Public registration: bot challenge, ``campus_register_ip`` / ``campus_register_drive`` rate limits (tenant settings,
bounded), the active ``campus`` notice (stale -> 409 ``notice_outdated``), optional ``marketing`` consent, answers
validated by the drive form (422 per field), Candidate linked by exact email or created (source ``campus``) and then
checked by P1 fuzzy duplicate detection (review queue, never merged). A second registration for the same drive returns
``already_registered`` and reveals nothing else. Eligibility runs after commit (``RegistrationReceived``).
"""

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, bot_challenge, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.models.campus import (
    REGISTRATION_TERMINAL,
    CampusDrive,
    DriveInstitution,
    Institution,
    Registration,
    RegistrationInvite,
)
from app.schemas.campus import EligibilityOut, FailingRule, RegistrationOut, RegistrationPage
from app.schemas.campus_public import (
    PublicDriveView,
    PublicFormField,
    PublicNotice,
    PublicRegisterIn,
    PublicRegisterOut,
)
from app.services.campus import forms
from app.services.campus.common import (
    CONSENT_PURPOSE,
    CONSENT_SOURCE_REGISTRATION,
    FILE_OWNER,
    LINK_REGISTRATION,
    SUBJECT_DRIVE_INSTITUTION,
    SUBJECT_INVITE,
    candidate_facts,
    conflict,
    drive_for,
    institution_names,
    invalid,
    now,
    settings_row,
)
from app.services.campus.domain_events import RegistrationReceived

_SYSTEM = frozenset({"system"})
_USER = frozenset({"user"})
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("registered", "eligible"): _SYSTEM,
    ("registered", "not_eligible"): _SYSTEM,
    ("eligible", "not_eligible"): _SYSTEM,
    ("not_eligible", "eligible"): _SYSTEM | _USER,
    ("eligible", "assessed"): _SYSTEM,
    ("assessed", "shortlisted"): _USER,
    ("shortlisted", "interviewed"): _SYSTEM,
    ("interviewed", "offered"): _SYSTEM | _USER,
    ("offered", "joined"): _SYSTEM,
    ("offered", "reneged"): _SYSTEM | _USER,
    ("offered", "rejected"): _SYSTEM | _USER,
}
WITHDRAW_ACTORS = frozenset({"user", "candidate", "system"})
MILESTONES = {
    "eligible": "eligible_at",
    "assessed": "assessed_at",
    "shortlisted": "shortlisted_at",
    "interviewed": "interviewed_at",
    "offered": "offered_at",
    "joined": "joined_at",
    "reneged": "reneged_at",
    "rejected": "rejected_at",
    "withdrawn": "withdrawn_at",
}
ACCEPTING = ("registration_open", "in_progress")
FILE_KINDS = frozenset({"pdf", "docx", "png", "jpeg"})


def allowed(src: str, dst: str, actor: str) -> bool:
    if dst == "withdrawn":
        return src not in REGISTRATION_TERMINAL and actor in WITHDRAW_ACTORS
    return actor in TRANSITIONS.get((src, dst), frozenset())


def set_status(reg: Registration, dst: str, actor: str, *, at: datetime | None = None) -> None:
    """The only writer of ``registrations.status`` (409 ``transition_not_allowed``)."""
    if not allowed(reg.status, dst, actor):
        raise conflict(
            "transition_not_allowed",
            f"A registration cannot move from {reg.status.replace('_', ' ')} to {dst.replace('_', ' ')}",
            status=reg.status,
            to=dst,
        )
    if reg.status == "eligible" and dst == "not_eligible":
        reg.eligible_at = None
    reg.status = dst
    col = MILESTONES.get(dst)
    if col is not None and getattr(reg, col) is None:
        setattr(reg, col, at or now())


# ================================================================ read models
def _eligibility(raw: dict[str, Any] | None) -> EligibilityOut | None:
    if not raw:
        return None
    fr = raw.get("failing_rule")
    return EligibilityOut(
        eligible=bool(raw.get("eligible")),
        failing_rule=FailingRule(**fr) if isinstance(fr, dict) else None,
        evaluated_at=raw.get("evaluated_at"),
        definition_version=raw.get("definition_version"),
        revision=raw.get("revision"),
    )


async def registration_outs(db: AsyncSession, rows: list[Registration]) -> list[RegistrationOut]:
    from app.models.assessments import AssessmentInvitation
    from app.models.offers import Offer

    facts = await candidate_facts(db, [r.candidate_id for r in rows])
    names = await institution_names(db, [r.institution_id for r in rows])
    inv_ids = [r.assessment_invitation_id for r in rows if r.assessment_invitation_id]
    inv_status = dict((await db.execute(select(AssessmentInvitation.id, AssessmentInvitation.status).where(AssessmentInvitation.id.in_(inv_ids)))).tuples().all()) if inv_ids else {}
    offer_ids = [r.offer_id for r in rows if r.offer_id]
    offer_status = dict((await db.execute(select(Offer.id, Offer.status).where(Offer.id.in_(offer_ids)))).tuples().all()) if offer_ids else {}
    out: list[RegistrationOut] = []
    for r in rows:
        name, email = facts.get(r.candidate_id, (None, None))
        out.append(
            RegistrationOut(
                id=r.id, drive_id=r.drive_id, institution_id=r.institution_id, institution_name=names.get(r.institution_id),
                candidate_id=r.candidate_id, candidate_name=name, candidate_email=email, status=r.status,
                source=r.source, program=r.program, branch=r.branch, cgpa=r.cgpa, percentage=r.percentage,
                backlogs=r.backlogs, graduation_year=r.graduation_year, answers=dict(r.answers or {}),
                eligibility=_eligibility(r.eligibility), overridden=r.override_at is not None,
                override_reason=r.override_reason, override_at=r.override_at,
                assessment_invitation_id=r.assessment_invitation_id,
                assessment_status=inv_status.get(r.assessment_invitation_id) if r.assessment_invitation_id else None,
                assessment_flag=r.assessment_flag, score=r.score, application_id=r.application_id, offer_id=r.offer_id,
                offer_status=offer_status.get(r.offer_id) if r.offer_id else None,
                planned_joining_date=r.planned_joining_date, registered_at=r.registered_at, eligible_at=r.eligible_at,
                assessed_at=r.assessed_at, shortlisted_at=r.shortlisted_at, interviewed_at=r.interviewed_at,
                offered_at=r.offered_at, accepted_at=r.accepted_at, joined_at=r.joined_at, reneged_at=r.reneged_at,
                rejected_at=r.rejected_at, withdrawn_at=r.withdrawn_at,
            )
        )
    return out


async def registration_for(c: RequestContext, db: AsyncSession, registration_id: uuid.UUID, *, write: bool = False, lock: bool = False) -> Registration:
    stmt = select(Registration).where(Registration.id == registration_id)
    if lock:
        stmt = stmt.with_for_update(of=Registration)
    reg = (await db.execute(stmt)).scalar_one_or_none()
    if reg is None:
        raise NotFound("Registration not found")
    try:
        await drive_for(c, db, reg.drive_id, write=write)
    except NotFound as exc:
        raise NotFound("Registration not found") from exc
    return reg


async def get_registration(c: RequestContext, db: AsyncSession, registration_id: uuid.UUID) -> RegistrationOut:
    reg = await registration_for(c, db, registration_id)
    return (await registration_outs(db, [reg]))[0]


async def list_registrations(
    c: RequestContext,
    db: AsyncSession,
    drive_id: uuid.UUID,
    *,
    statuses: list[str] | None = None,
    institution_id: uuid.UUID | None = None,
    q: str | None = None,
    flagged: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> RegistrationPage:
    from app.models.candidates import Candidate

    drive = await drive_for(c, db, drive_id)
    stmt = select(Registration).where(Registration.drive_id == drive.id)
    if statuses:
        stmt = stmt.where(Registration.status.in_(statuses))
    if institution_id is not None:
        stmt = stmt.where(Registration.institution_id == institution_id)
    if flagged is not None:
        stmt = stmt.where(Registration.assessment_flag.is_not(None) if flagged else Registration.assessment_flag.is_(None))
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            Registration.candidate_id.in_(select(Candidate.id).where(Candidate.full_name.ilike(like, escape="\\")))
        )
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = list(
        (await db.execute(stmt.order_by(Registration.score.desc().nulls_last(), Registration.registered_at, Registration.id).limit(limit).offset(offset))).scalars()
    )
    return RegistrationPage(items=await registration_outs(db, rows), total=total, limit=limit, offset=offset)


async def withdraw(c: RequestContext, db: AsyncSession, registration_id: uuid.UUID, note: str | None) -> RegistrationOut:
    reg = await registration_for(c, db, registration_id, write=True, lock=True)
    set_status(reg, "withdrawn", "user")
    if note:
        reg.note = note[:1000]
    await db.flush()
    audit.record("campus.registration_withdrawn", "campus_registration", reg.id, session=db)
    return (await registration_outs(db, [reg]))[0]


# ================================================================ public page
def _ip_hash(ip: str | None) -> str:
    return hashlib.sha256((ip or "unknown").encode()).hexdigest()[:32]


async def _notice(db: AsyncSession, tenant_id: uuid.UUID, purpose: str) -> PublicNotice | None:
    from app.services.privacy import consent

    row = await consent.active_notice(db, tenant_id, purpose)
    if row is not None:
        return PublicNotice(notice_id=row.id, version=row.version, title=row.title, body_md=row.body_md)
    default = consent.DEFAULT_NOTICES.get(purpose)
    if default is None:
        return None
    return PublicNotice(notice_id=None, version=None, title=default[0], body_md=default[1])


async def _resolve(token: str) -> secure_links.ResolvedLink:
    return await secure_links.resolve(token, (LINK_REGISTRATION,))


async def _context(db: AsyncSession, link: secure_links.ResolvedLink) -> tuple[CampusDrive, DriveInstitution, Institution, RegistrationInvite | None]:
    invite: RegistrationInvite | None = None
    if link.subject_type == SUBJECT_INVITE:
        invite = await db.get(RegistrationInvite, link.subject_id)
        if invite is None:
            raise NotFound("Link not found", code="link_invalid")
        di_id = invite.drive_institution_id
    elif link.subject_type == SUBJECT_DRIVE_INSTITUTION:
        di_id = link.subject_id
    else:
        raise NotFound("Link not found", code="link_invalid")
    di = await db.get(DriveInstitution, di_id)
    if di is None:
        raise NotFound("Link not found", code="link_invalid")
    drive = await db.get(CampusDrive, di.drive_id)
    inst = await db.get(Institution, di.institution_id)
    if drive is None or inst is None:
        raise NotFound("Link not found", code="link_invalid")
    return drive, di, inst, invite


def _open(drive: CampusDrive, di: DriveInstitution) -> bool:
    if drive.status not in ACCEPTING or di.status != "active":
        return False
    return not (drive.registration_closes_at is not None and drive.registration_closes_at <= now())


async def public_view(token: str, *, ip: str | None) -> PublicDriveView:
    from app.services import identity_bridge

    await rate_limit.enforce("campus_view_ip", _ip_hash(ip), 120, 3600)
    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        drive, di, inst, invite = await _context(db, link)
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        notice = await _notice(db, link.tenant_id, CONSENT_PURPOSE)
        assert notice is not None
        fields = forms.fields_of(drive.form_schema)
        programs = list(inst.programs or [])
        out_fields = []
        for f in fields:
            options = f.options
            if f.key == "program":
                options = [str(p.get("name")) for p in programs if p.get("name")] or None
                if options is None:
                    f = f.model_copy(update={"type": "text"})
            if f.key == "branch" and not any(p.get("branches") for p in programs):
                f = f.model_copy(update={"type": "text"})
            out_fields.append(
                PublicFormField(
                    key=f.key, label=f.label, type=f.type, required=f.required, options=options,
                    min=str(f.min) if f.min is not None else None, max=str(f.max) if f.max is not None else None, help=f.help,
                )
            )
        prefill: dict[str, Any] = {}
        if invite is not None:
            prefill = {"email": invite.email, "full_name": invite.name, **(invite.prefill or {})}
        return PublicDriveView(
            status="open" if _open(drive, di) else "closed",
            tenant_name=tenant.name if tenant is not None else None,
            drive_name=drive.name,
            institution_name=str(inst.name),
            batch_year=drive.batch_year,
            closes_at=drive.registration_closes_at,
            fields=out_fields,
            branches_by_program=forms.branches_by_program(programs),
            notice=notice,
            marketing_notice=await _notice(db, link.tenant_id, "marketing"),
            prefill={k: v for k, v in prefill.items() if v is not None},
            challenge=bot_challenge.public_config(),
        )


async def _check_notice(db: AsyncSession, tenant_id: uuid.UUID, notice_id: uuid.UUID | None, agreed: bool) -> PublicNotice:
    current = await _notice(db, tenant_id, CONSENT_PURPOSE)
    assert current is not None
    if not agreed:
        raise invalid("consent_required", "Read and accept the notice to continue", {"agreed": "Required"})
    if current.notice_id is not None:
        if notice_id is None:
            raise invalid("notice_required", "The notice id is required", {"notice_id": "Required"})
        if notice_id != current.notice_id:
            raise ConflictError(
                "The notice changed; please read it again",
                code="notice_outdated",
                details={"notice_id": str(current.notice_id)},
            )
    return current


async def find_or_create_candidate(
    c: RequestContext, db: AsyncSession, *, full_name: str, email: str, phone: str | None, owner_id: uuid.UUID, source: str, source_detail: str
) -> tuple[uuid.UUID, bool]:
    """P1 duplicate detection: exact email links the existing Candidate; otherwise a new Candidate is created and
    fuzzy pairs are queued for review (never merged). Returns ``(candidate_id, created)``."""
    from app.schemas.candidates import CandidateCreate, EmailIn, PhoneIn
    from app.services.candidates import duplicates, records
    from app.services.candidates.common import normalize_phone

    existing = await duplicates.link_by_exact_email(c, db, email)
    if existing is not None:
        return existing, False
    norm_phone = normalize_phone(phone) if phone else None
    data = CandidateCreate(
        full_name=full_name.strip(),
        source_detail=source_detail[:200],
        emails=[EmailIn(email=email, is_primary=True)],
        phones=[PhoneIn(phone=norm_phone, is_primary=True)] if norm_phone else [],
    )
    cand, _ = await records.create_record(c, db, data, owner_id=owner_id, source=source)
    await duplicates.find_duplicates(c, db, cand)
    return cand.id, True


async def record_consents(
    db: AsyncSession, candidate_id: uuid.UUID, *, notice: PublicNotice, marketing: bool, source: str, ip: str | None, evidence: dict[str, Any]
) -> uuid.UUID:
    from app.services import privacy

    ev = {**evidence, "ip_hash": _ip_hash(ip)}
    row = await privacy.consent.record(db, candidate_id, CONSENT_PURPOSE, source, notice.version, None, evidence=ev)
    if marketing:
        mk = await _notice(db, row.tenant_id, "marketing")
        await privacy.consent.record(db, candidate_id, "marketing", source, mk.version if mk else None, None, evidence=ev)
    return row.id


async def public_register(
    token: str,
    payload: PublicRegisterIn,
    files: dict[str, tuple[bytes, str | None]],
    *,
    ip: str | None,
    user_agent: str | None,
) -> PublicRegisterOut:
    from app.services import files_service
    from app.services.candidates.common import normalize_email

    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        settings = await settings_row(db)
        ip_limit, drive_limit = settings.register_ip_limit, settings.register_drive_limit
        drive, di, inst, invite = await _context(db, link)
        drive_id = drive.id
    await rate_limit.enforce("campus_register_ip", _ip_hash(ip), ip_limit, 3600)
    await rate_limit.enforce("campus_register_drive", drive_id.hex, drive_limit, 600)
    await bot_challenge.verify_or_fail(payload.bot_token, ip)
    email = normalize_email(str(payload.email))
    if email is None:
        raise invalid("invalid_email", "Enter a valid email address", {"email": "Enter a valid email address"})
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "system", None)
        drive, di, inst, invite = await _context(db, link)
        if not _open(drive, di):
            raise conflict("registration_closed", "Registration for this drive is closed")
        notice = await _check_notice(db, link.tenant_id, payload.notice_id, payload.agreed)
        answers, typed = forms.validate_answers(drive.form_schema, payload.answers, list(inst.programs or []), set(files))
        c = RequestContext(tenant_id=link.tenant_id, ip=ip, user_agent=user_agent)
        candidate_id, _created = await find_or_create_candidate(
            c, db, full_name=payload.full_name, email=email, phone=payload.phone, owner_id=drive.owner_id,
            source="campus", source_detail=f"Campus drive: {drive.name}",
        )
        dup = (await db.execute(select(Registration.id).where(Registration.drive_id == drive.id, Registration.candidate_id == candidate_id))).first()
        if dup is not None:
            return PublicRegisterOut(status="already_registered")
        reg = Registration(
            id=uuid.uuid4(),
            tenant_id=link.tenant_id,
            drive_id=drive.id,
            drive_institution_id=di.id,
            institution_id=di.institution_id,
            candidate_id=candidate_id,
            status="registered",
            answers=dict(answers),
            batch_year=drive.batch_year,
            source="import" if invite is not None else "link",
            registered_at=now(),
            **typed,
        )
        try:
            async with db.begin_nested():
                db.add(reg)
                await db.flush()
        except IntegrityError:
            return PublicRegisterOut(status="already_registered")
        audit.set_actor(db, "candidate", candidate_id)
        for key, (data, filename) in files.items():
            if answers.get(key) != "__file__":
                continue
            stored = await files_service.store_upload(c, db, data, filename, FILE_KINDS, FILE_OWNER, reg.id)
            answers[key] = str(stored.id)
        reg.answers = dict(answers)
        reg.consent_id = await record_consents(
            db, candidate_id, notice=notice, marketing=payload.marketing_opt_in, source=CONSENT_SOURCE_REGISTRATION,
            ip=ip, evidence={"drive_id": str(drive.id), "registration_id": str(reg.id)},
        )
        await secure_links.consume(db, link.id)
        if invite is not None:
            invite.status = "registered"
            invite.registration_id = reg.id
        await db.flush()
        emit(db, RegistrationReceived(tenant_id=link.tenant_id, drive_id=drive.id, registration_id=reg.id))
        audit.record("campus.registered", "campus_registration", reg.id, session=db, drive_id=str(drive.id), source=reg.source)
    return PublicRegisterOut(status="registered")
