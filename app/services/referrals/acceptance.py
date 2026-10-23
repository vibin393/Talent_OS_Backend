# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Referee acceptance (Epic 42 story 42.2, FR-214, FR-21) and the public referee page (Secure Link purpose
``referral_link``). No session: the token is the credential and is never logged.

* ``view`` -- upload channel (``subject_type='referral'``): the notice with Accept / Decline; share channel
  (``subject_type='referral_share'``): the self-submit form with the notice. Viewing never consumes a use.
* ``accept`` / ``decline`` -- one-shot (the link is single use). Decline (and expiry, and withdraw) minimise the
  referral within the request: the resume file is tombstoned and the referee's contact data is nulled.
* ``share_submit`` -- bot challenge, per-IP and per-link rate limits; creates the referral and accepts it in one
  step (the referee submits and agrees together).

``process_acceptance`` runs in one transaction: ``grant_consent(purpose='referral', source='referral_link')``,
duplicate detection (exact email, then ``probe_matches``) -- an existing Candidate with activity in the last
``duplicate_activity_months`` makes the referral a ``duplicate`` (not reward-eligible, no application, the
recruiters are told); a dormant one is reused (new resume version); otherwise a ``referral`` Candidate is created.
With a Demand that accepts applications the application is created with ``source='referral'``,
``source_ref=str(referral.id)``, actor ``candidate`` -> ``in_process``; a general referral stays ``accepted`` until
a Recruiter links it to a Demand (``link_demand``).

Resumes are text-extracted only; no AI parse runs on a referral (the ``referral`` notice does not cover AI
processing) -- recruiters can parse later through the P1 resume flow.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, bot_challenge, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.models.referrals import Referral, ReferralShareLink
from app.schemas.referrals import PublicNotice, PublicReferralOut, PublicReferralView
from app.services.referrals import eligibility
from app.services.referrals.common import (
    CONSENT_PURPOSE,
    CONSENT_SOURCE,
    FILE_OWNER,
    LINK_PURPOSE,
    REF_MANAGE,
    SUBJECT_REFERRAL,
    SUBJECT_SHARE,
    Referrer,
    conflict,
    get_settings_row,
    invalid,
    now,
    require,
    tenant_of,
)
from app.services.referrals.events import ReferralDuplicateFound, ReferralReceived

PURPOSES = (LINK_PURPOSE,)


def _ip_hash(ip: str | None) -> str:
    return hashlib.sha256((ip or "unknown").encode()).hexdigest()[:32]


def months_before(at: datetime, months: int) -> datetime:
    """``at`` minus whole calendar months (day clamped to the target month's length)."""
    import calendar

    y, m = divmod(at.month - 1 - months, 12)
    year, month = at.year + y, m + 1
    day = min(at.day, calendar.monthrange(year, month)[1])
    return at.replace(year=year, month=month, day=day)


# ================================================================ minimisation
async def minimise(db: AsyncSession, referral: Referral) -> None:
    """Tombstone the resume and null the referee's contact data (decline / expiry / withdraw)."""
    from app.services import files_service

    if referral.resume_file_id is not None:
        await files_service.purge_file(db, referral.resume_file_id)
    referral.referee_name = None
    referral.referee_email = None
    referral.referee_phone = None
    referral.relationship = None
    referral.note = None
    await db.flush()


# ================================================================ duplicate activity (P1 / P2 public facts)
async def last_activity_since(db: AsyncSession, candidate_id: uuid.UUID, since: datetime) -> bool:
    """True when the Candidate had recruiting activity at or after ``since``: an application created or a stage
    change, a Submission, a Placement, or a recruiter note."""
    from app.models.activity import Activity
    from app.models.pipeline import Application, ApplicationStageHistory, Submission
    from app.models.placements import Placement

    probes = (
        select(Application.id).where(
            Application.candidate_id == candidate_id, Application.created_at >= since
        ),
        select(ApplicationStageHistory.id)
        .join(Application, Application.id == ApplicationStageHistory.application_id)
        .where(Application.candidate_id == candidate_id, ApplicationStageHistory.at >= since),
        select(Submission.id).where(Submission.candidate_id == candidate_id, Submission.created_at >= since),
        select(Placement.id).where(Placement.candidate_id == candidate_id, Placement.created_at >= since),
        select(Activity.id).where(
            Activity.entity_type == "candidate",
            Activity.entity_id == candidate_id,
            Activity.kind == "note",
            Activity.author_id.is_not(None),
            Activity.occurred_at >= since,
        ),
    )
    for stmt in probes:
        if (await db.execute(stmt.limit(1))).first() is not None:
            return True
    return False


async def find_existing(ctx: RequestContext, db: AsyncSession, referral: Referral) -> uuid.UUID | None:
    """FR-21: exact email first, then the duplicate probe (exact phone / email)."""
    from app.models.candidates import Candidate
    from app.services.candidates import duplicates
    from app.services.candidates.common import normalize_email, normalize_phone

    email = str(referral.referee_email or "")
    found = await duplicates.link_by_exact_email(ctx, db, email) if email else None
    if found is not None:
        return found
    phone = normalize_phone(referral.referee_phone) if referral.referee_phone else None
    norm = normalize_email(email) if email else None
    probe = duplicates.Probe(
        full_name=referral.referee_name, emails=(norm,) if norm else (), phones=(phone,) if phone else ()
    )
    matches = await duplicates.probe_matches(db, probe)
    exact = sorted(
        (cid for cid, (reasons, _) in matches.items() if reasons & {"exact_email", "exact_phone"}), key=str
    )
    for cid in exact:
        cand = await db.get(Candidate, cid)
        if cand is not None and cand.erased_at is None and cand.merged_into_id is None:
            return cid
    return None


async def _owner_for(db: AsyncSession, referral: Referral) -> uuid.UUID:
    """P1 FR-24: the Demand's primary assignee, else the default owner, else the referring User, else the Demand
    owner, else a Tenant Admin."""
    from app.models.demands import Demand, DemandAssignment
    from app.services import identity_bridge

    if referral.demand_id is not None:
        primary = (
            await db.execute(
                select(DemandAssignment.user_id).where(
                    DemandAssignment.demand_id == referral.demand_id,
                    DemandAssignment.is_primary.is_(True),
                    DemandAssignment.removed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if primary is not None:
            return primary
    settings = await get_settings_row(db)
    if settings.default_owner_user_id is not None:
        return settings.default_owner_user_id
    if referral.referrer_user_id is not None:
        return referral.referrer_user_id
    if referral.demand_id is not None:
        d = await db.get(Demand, referral.demand_id)
        if d is not None:
            return d.owner_id
    admins = await identity_bridge.tenant_admins(db)
    if not admins:  # pragma: no cover - every tenant has an admin
        msg = "no owner available for the referred candidate"
        raise RuntimeError(msg)
    return uuid.UUID(str(admins[0]["id"]))


async def _resume_bytes(referral: Referral, db: AsyncSession) -> tuple[bytes | None, Any]:
    from app.services import files_service

    if referral.resume_file_id is None:
        return None, None
    file = await files_service.get_file(db, referral.resume_file_id)
    if file is None or file.deleted_at is not None:
        return None, None
    try:
        return await files_service.read_file(file), file
    except FileNotFoundError:  # pragma: no cover - storage drift
        return None, file


async def _attach_resume(
    ctx: RequestContext, db: AsyncSession, referral: Referral, candidate_id: uuid.UUID
) -> None:
    from app.services.candidates import portal_port

    data, file = await _resume_bytes(referral, db)
    if file is None:
        return
    extraction = portal_port.extract(data) if data else None
    await portal_port.add_resume(
        ctx,
        db,
        candidate_id,
        file_id=file.id,
        text=extraction.text if extraction else None,
        text_status=extraction.status if extraction else "failed",
        parse_status="skipped_consent",
        parsed_data=None,
        ai_action_id=None,
        note=f"Resume received with referral {referral.human_id}",
    )


async def _create_candidate(ctx: RequestContext, db: AsyncSession, referral: Referral) -> uuid.UUID:
    from app.schemas.candidates import CandidateCreate, EmailIn, PhoneIn
    from app.services.candidates import duplicates, records
    from app.services.candidates.common import normalize_phone

    phone = normalize_phone(referral.referee_phone) if referral.referee_phone else None
    data = CandidateCreate(
        full_name=referral.referee_name or "Referred candidate",
        source="referral",
        source_detail=f"Referral {referral.human_id}",
        emails=[EmailIn(email=str(referral.referee_email), is_primary=True)],
        phones=[PhoneIn(phone=phone, is_primary=True)] if phone else [],
    )
    cand, _ = await records.create_record(
        ctx, db, data, owner_id=await _owner_for(db, referral), source="referral"
    )
    await duplicates.find_duplicates(ctx, db, cand)
    return cand.id


async def _apply(db: AsyncSession, referral: Referral, cctx: RequestContext) -> uuid.UUID | None:
    """Create the referral application (``source='referral'``, actor ``candidate``) when the Demand accepts
    applications; None when it does not, or the Candidate already has an application on it."""
    from app.models.demands import Demand
    from app.services.pipeline import portal_port as pipeline

    if referral.demand_id is None or referral.candidate_id is None:
        return None
    d = await db.get(Demand, referral.demand_id)
    if d is None or d.status not in ("open", "on_hold") or d.archived_at is not None:
        return None
    if await pipeline.existing_application(db, referral.demand_id, referral.candidate_id) is not None:
        return None
    return await pipeline.create_candidate_application(
        cctx, db, referral.demand_id, referral.candidate_id, source="referral", source_ref=str(referral.id)
    )


async def process_acceptance(
    db: AsyncSession,
    referral: Referral,
    *,
    notice_id: uuid.UUID | None,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """The one-transaction acceptance (see the module docstring). Returns the resulting status."""
    from app.services import privacy
    from app.services.referrals.submit import set_status

    tid = tenant_of(db)
    ctx = RequestContext(tenant_id=tid, ip=ip, user_agent=user_agent)
    audit.set_actor(db, "system", None)
    settings = await get_settings_row(db)
    referral.accepted_at = now()
    set_status(db, referral, "accepted")
    existing = await find_existing(ctx, db, referral)
    duplicate = False
    if existing is not None:
        since = months_before(datetime.now(UTC), settings.duplicate_activity_months)
        duplicate = await last_activity_since(db, existing, since)
        candidate_id = existing
    else:
        candidate_id = await _create_candidate(ctx, db, referral)
    referral.candidate_id = candidate_id
    audit.set_actor(db, "candidate", candidate_id)
    cctx = RequestContext(tenant_id=tid, ip=ip, user_agent=user_agent)
    consent = await privacy.grant_consent(
        cctx,
        db,
        candidate_id,
        CONSENT_PURPOSE,
        source=CONSENT_SOURCE,
        notice_id=notice_id,
        evidence={
            "referral_id": str(referral.id),
            "notice_id": str(notice_id) if notice_id else None,
            "ip_hash": _ip_hash(ip),
        },
    )
    referral.consent_id = consent.id
    if duplicate:
        referral.reward_eligible = False
        referral.ineligible_reason = "duplicate"
        set_status(db, referral, "duplicate")
        emit(
            db,
            ReferralDuplicateFound(
                tenant_id=tid,
                referral_id=referral.id,
                demand_id=referral.demand_id,
                candidate_id=candidate_id,
            ),
        )
        await db.flush()
        audit.record(
            "referral.duplicate", "referral", referral.id, session=db, candidate_id=str(candidate_id)
        )
        return referral.status
    await _attach_resume(ctx, db, referral, candidate_id)
    application_id = await _apply(db, referral, cctx)
    if application_id is not None:
        referral.application_id = application_id
        set_status(db, referral, "in_process")
    elif referral.demand_id is not None and existing is not None:
        # Dormant Candidate already holding an application on this Demand: nothing new to process.
        referral.reward_eligible = False
        referral.ineligible_reason = "duplicate"
        set_status(db, referral, "duplicate")
    await db.flush()
    emit(db, ReferralReceived(tenant_id=tid, referral_id=referral.id, demand_id=referral.demand_id))
    audit.record("referral.accepted", "referral", referral.id, session=db, status=referral.status)
    return referral.status


# ================================================================ public page
async def _notice(db: AsyncSession) -> PublicNotice:
    from app.services.privacy import consent

    row = await consent.active_notice(db, tenant_of(db), CONSENT_PURPOSE)
    if row is not None:
        return PublicNotice(notice_id=row.id, version=row.version, title=row.title, body_md=row.body_md)
    title, body = consent.DEFAULT_NOTICES.get(CONSENT_PURPOSE, ("Referral", ""))
    return PublicNotice(notice_id=None, version=None, title=title, body_md=body)


async def _referrer_first_name(db: AsyncSession, kind: str, rid: uuid.UUID | None) -> str | None:
    from app.services.referrals.common import referrer_names

    if rid is None:
        return None
    names = await referrer_names(db, {rid} if kind == "user" else set(), {rid} if kind == "worker" else set())
    full = names.get(rid)
    return full.split(" ")[0] if full else None


async def _job_title(db: AsyncSession, demand_id: uuid.UUID | None) -> str | None:
    from app.models.demands import Demand

    if demand_id is None:
        return None
    d = await db.get(Demand, demand_id)
    return d.title if d is not None else None


async def _share_row(db: AsyncSession, link: secure_links.ResolvedLink) -> ReferralShareLink:
    row = await db.get(ReferralShareLink, link.subject_id)
    if row is None or row.revoked_at is not None:
        raise NotFound("Link not found", code="link_invalid")
    return row


async def link_summary(link: secure_links.ResolvedLink) -> tuple[str, str | None, str | None]:
    """(status, tenant name, job title) for the generic ``GET /public/links/{token}`` frame."""
    from app.services import identity_bridge

    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        if link.subject_type == SUBJECT_REFERRAL:
            r = await db.get(Referral, link.subject_id)
            status = "open" if r is not None and r.status == "submitted" else "answered"
            title = await _job_title(db, r.demand_id) if r is not None else None
        else:
            row = await db.get(ReferralShareLink, link.subject_id)
            status = "open" if row is not None and row.revoked_at is None else "closed"
            title = await _job_title(db, row.demand_id) if row is not None else None
    return status, tenant.name if tenant is not None else None, title


async def view(token: str) -> PublicReferralView:
    from app.services import identity_bridge

    link = await secure_links.resolve(token, PURPOSES)
    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        notice = await _notice(db)
        if link.subject_type == SUBJECT_REFERRAL:
            r = await db.get(Referral, link.subject_id)
            if r is None:
                raise NotFound("Link not found", code="link_invalid")
            kind, rid = r.referrer_kind, r.referrer_user_id or r.referrer_candidate_id
            demand_id, mode = r.demand_id, "accept"
            status = "open" if r.status == "submitted" else "answered"
            referee_first = (r.referee_name or "").split(" ")[0] or None
        elif link.subject_type == SUBJECT_SHARE:
            row = await _share_row(db, link)
            kind, rid = row.referrer_kind, row.referrer_user_id or row.referrer_candidate_id
            demand_id, mode, status, referee_first = row.demand_id, "submit", "open", None
        else:
            raise NotFound("Link not found", code="link_invalid")
        return PublicReferralView(
            mode=mode,
            status=status,
            tenant_name=tenant.name if tenant is not None else None,
            job_title=await _job_title(db, demand_id),
            general=demand_id is None,
            referrer_first_name=await _referrer_first_name(db, kind, rid),
            referee_first_name=referee_first,
            notice=notice,
            expires_at=link.expires_at,
        )


async def _check_notice(db: AsyncSession, notice_id: uuid.UUID | None) -> uuid.UUID | None:
    current = await _notice(db)
    if notice_id is not None and current.notice_id is not None and notice_id != current.notice_id:
        raise ConflictError(
            "The notice changed; please read it again",
            code="notice_outdated",
            details={"notice_id": str(current.notice_id)},
        )
    return current.notice_id


async def _upload_referral(db: AsyncSession, link: secure_links.ResolvedLink) -> Referral:
    if link.subject_type != SUBJECT_REFERRAL:
        raise NotFound("Link not found", code="link_invalid")
    r = await db.get(Referral, link.subject_id, with_for_update=True)
    if r is None:
        raise NotFound("Link not found", code="link_invalid")
    if r.status != "submitted":
        raise conflict("referral_answered", "This referral was already answered")
    return r


async def accept(
    token: str, notice_id: uuid.UUID | None, *, ip: str | None, user_agent: str | None
) -> PublicReferralOut:
    link = await secure_links.resolve(token, PURPOSES)
    async with tenant_session(link.tenant_id) as db:
        r = await _upload_referral(db, link)
        nid = await _check_notice(db, notice_id)
        await secure_links.consume(db, link.id)
        status = await process_acceptance(db, r, notice_id=nid, ip=ip, user_agent=user_agent)
    return PublicReferralOut(status="accepted" if status != "declined" else status)


async def decline(token: str) -> PublicReferralOut:
    from app.services.referrals.submit import set_status

    link = await secure_links.resolve(token, PURPOSES)
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "system", None)
        r = await _upload_referral(db, link)
        await secure_links.consume(db, link.id)
        await minimise(db, r)
        set_status(db, r, "declined")
        await db.flush()
        audit.record("referral.declined", "referral", r.id, session=db)
    return PublicReferralOut(status="declined")


async def _share_referrer(db: AsyncSession, row: ReferralShareLink) -> Referrer:
    from app.models.candidates import Candidate
    from app.models.identity import User
    from app.services.candidates import portal_port

    if row.referrer_kind == "user":
        u = await db.get(User, row.referrer_user_id)
        if u is None or not u.is_active:
            raise NotFound("Link not found", code="link_invalid")
        return Referrer("user", u.id, row.tenant_id, str(u.email), u.name)
    cand = await db.get(Candidate, row.referrer_candidate_id)
    if cand is None or cand.erased_at is not None:
        raise NotFound("Link not found", code="link_invalid")
    email = await portal_port.primary_email(db, cand.id)
    return Referrer("worker", cand.id, row.tenant_id, email, cand.full_name)


async def share_submit(
    token: str,
    *,
    name: str,
    email: str,
    phone: str | None,
    resume: bytes,
    filename: str | None,
    notice_id: uuid.UUID | None,
    agreed: bool,
    bot_token: str | None,
    ip: str | None,
    user_agent: str | None,
) -> PublicReferralOut:
    """Share channel: the referee submits their details and accepts the notice in one step."""
    from app.services import files_service
    from app.services.candidates.common import normalize_email
    from app.services.referrals.submit import HUMAN_ID_KEY, HUMAN_ID_PREFIX, flush_new, new_referral, prepare

    await rate_limit.enforce("referral_share_ip", _ip_hash(ip), 10, 3600)
    if not agreed:
        raise invalid("consent_required", "Read and accept the notice to continue")
    link = await secure_links.resolve(token, PURPOSES)
    if link.subject_type != SUBJECT_SHARE:
        raise NotFound("Link not found", code="link_invalid")
    await rate_limit.enforce("referral_share_link", link.id.hex, 50, 3600)
    await bot_challenge.verify_or_fail(bot_token, ip)
    clean = normalize_email(email)
    if clean is None:
        raise invalid("invalid_email", "Invalid email address", fields={"email": "Invalid email address"})
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "system", None)
        row = await _share_row(db, link)
        referrer = await _share_referrer(db, row)
        _, program = await prepare(db, referrer, row.demand_id, clean)
        nid = await _check_notice(db, notice_id)
        referral_id = uuid.uuid4()
        ctx = RequestContext(tenant_id=link.tenant_id, ip=ip, user_agent=user_agent)
        file = await files_service.store_upload(
            ctx, db, resume, filename, files_service.RESUME_KINDS, FILE_OWNER, referral_id
        )
        referral = new_referral(
            referral_id,
            link.tenant_id,
            await next_human_id(db, HUMAN_ID_KEY, HUMAN_ID_PREFIX, width=6),
            referrer,
            row.demand_id,
            program,
            channel="share_link",
            name=name,
            email=clean,
            phone=phone,
            resume_file_id=file.id,
            share_link_id=row.id,
        )
        await flush_new(db, referral)
        await secure_links.consume(db, link.id)
        row.uses += 1
        from app.services.referrals.submit import status_event

        emit(db, status_event(referral, None))
        await process_acceptance(db, referral, notice_id=nid, ip=ip, user_agent=user_agent)
    return PublicReferralOut(status="received")


# ================================================================ expiry (beat) / link to demand
async def expire_due(db: AsyncSession) -> int:
    """``submitted`` referrals whose referee link expired -> ``expired`` (minimised)."""
    from app.core.secure_links import SecureLink
    from app.services.referrals.submit import set_status

    rows = list(
        (
            await db.execute(
                select(Referral)
                .join(SecureLink, SecureLink.id == Referral.secure_link_id)
                .where(Referral.status == "submitted", SecureLink.expires_at <= now())
                .with_for_update(of=Referral)
            )
        ).scalars()
    )
    for r in rows:
        await minimise(db, r)
        set_status(db, r, "expired")
    await db.flush()
    return len(rows)


async def link_demand(
    ctx: RequestContext, db: AsyncSession, referral_id: uuid.UUID, demand_id: uuid.UUID
) -> Any:
    """A Recruiter links an accepted general referral to a Demand (actor ``user``) -> application -> in_process.
    Program and exclusions are evaluated for the new Demand: an excluded referrer keeps the referral but loses the
    reward (``excluded_referrer``)."""
    from app.services import pipeline_service
    from app.services.referrals import tracking
    from app.services.referrals.submit import _demand_or_404, set_status

    require(ctx, REF_MANAGE)
    r = await tracking.visible_referral(ctx, db, referral_id, for_update=True)
    if r.status != "accepted" or r.demand_id is not None or r.candidate_id is None:
        raise conflict(
            "referral_not_linkable", "Only an accepted general referral can be linked to an opening"
        )
    d = await _demand_or_404(db, demand_id)
    referrer = await tracking.referrer_of(db, r)
    facts = await eligibility.demand_facts(db, d.id)
    program = eligibility.program_for(
        await eligibility.load_programs(db), facts, referrer.kind, tracking.today()
    )
    excluded = (
        program is not None and facts is not None and eligibility.exclusion_reason(program, facts, referrer)
    )
    r.demand_id = d.id
    r.program_id = program.id if program else None
    if program is None:
        r.reward_eligible, r.ineligible_reason = False, "no_program"
    elif excluded:
        r.reward_eligible, r.ineligible_reason = False, "excluded_referrer"
    else:
        r.reward_eligible, r.ineligible_reason = True, None
    app_id = await pipeline_service.add_candidate(
        ctx, db, d.id, r.candidate_id, source="referral", source_ref=str(r.id)
    )
    taken = (
        await db.execute(
            select(Referral.id).where(and_(Referral.application_id == app_id, Referral.id != r.id))
        )
    ).first()
    if taken is not None:
        raise conflict("application_exists", "The candidate already has a referral on this opening")
    r.application_id = app_id
    set_status(db, r, "in_process")
    await db.flush()
    return await tracking.internal_out(db, r)
