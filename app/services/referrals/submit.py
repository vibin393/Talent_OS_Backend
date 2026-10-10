# ruff: noqa: E501, D401
"""Refer a Candidate (Epic 42 story 42.2, FR-214): openings, the upload channel, personal share links, withdraw.

Upload channel: the referrer enters the referee's contact data and resume (scanned store, pdf / docx, <= 10 MB);
the referral is ``submitted`` and the referee gets a single-use Secure Link (purpose ``referral_link``,
``subject_type='referral'``) with the ``referral`` notice. **No Candidate record, parsing or AI happens before the
referee accepts** (FR-214, FR-256): until then only the referral row and the stored file exist, visible to the
referrer (referrer-safe view) and to ``referral:read`` users.

Share channel: a personal multi-use link per (referrer, demand) (purpose ``referral_link``,
``subject_type='referral_share'``); the referee submits and accepts in one step on the public page.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.encrypted import open_sealed, seal
from app.core.events import emit
from app.core.sequences import next_human_id
from app.models.referrals import CLOSED_STATUSES, Referral, ReferralShareLink
from app.schemas.referrals import ReferIn
from app.schemas.referrals_referrer import (
    ReferrerOpeningOut,
    ReferrerReferralOut,
    ReferrerShareLinkOut,
    status_label,
)
from app.services.referrals import eligibility
from app.services.referrals.common import (
    FILE_OWNER,
    LINK_PURPOSE,
    SHARE_LINK_MAX_USES,
    SUBJECT_REFERRAL,
    SUBJECT_SHARE,
    Referrer,
    conflict,
    get_settings_row,
    not_found,
    now,
    today,
)
from app.services.referrals.events import ReferralStatusChanged

ACCEPTING_DEMAND_STATUSES = ("open", "on_hold")
OPENING_STATUSES = ("open",)
HUMAN_ID_KEY = "referral"
HUMAN_ID_PREFIX = "REF"


# ================================================================ helpers shared with acceptance / tracking
def status_event(referral: Referral, from_status: str | None) -> ReferralStatusChanged:
    return ReferralStatusChanged(
        tenant_id=referral.tenant_id,
        referral_id=referral.id,
        from_status=from_status,
        to_status=referral.status,
        status_label=status_label(referral.status),
        referrer_kind=referral.referrer_kind,
        referrer_id=referral.referrer_user_id or referral.referrer_candidate_id,  # type: ignore[arg-type]
        demand_id=referral.demand_id,
    )


def set_status(db: AsyncSession, referral: Referral, status: str) -> None:
    """Change the status and emit ``ReferralStatusChanged`` (no-op when unchanged)."""
    before = referral.status
    if before == status:
        return
    referral.status = status
    if status in ("hired", "not_selected", "duplicate", "declined", "expired", "withdrawn"):
        referral.decided_at = now()
    emit(db, status_event(referral, before))


async def _demand_or_404(db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.models.demands import Demand

    d = await db.get(Demand, demand_id)
    if d is None or d.archived_at is not None:
        raise not_found("Opening")
    return d


async def _client_names(db: AsyncSession, demand_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Client names only where the live posting discloses the client (careers ``show_client_name``)."""
    from app.models.clients import Client
    from app.models.demands import Demand, DemandPosting

    if not demand_ids:
        return {}
    rows = await db.execute(
        select(DemandPosting.demand_id, Client.name)
        .join(Demand, Demand.id == DemandPosting.demand_id)
        .join(Client, Client.id == Demand.client_id)
        .where(
            DemandPosting.demand_id.in_(demand_ids),
            DemandPosting.is_live.is_(True),
            DemandPosting.show_client_name.is_(True),
        )
    )
    return dict(rows.tuples().all())


# ================================================================ openings
async def openings(db: AsyncSession, referrer: Referrer) -> list[ReferrerOpeningOut]:
    """Open Demands eligible under some program for this referrer, with job-card fields and the reward amount.
    Never rates, internal notes or Demands the referrer works on."""
    from app.models.demands import Demand

    programs = await eligibility.load_programs(db)
    if not programs:
        return []
    demands = list(
        (
            await db.execute(
                select(Demand)
                .where(Demand.status.in_(OPENING_STATUSES), Demand.archived_at.is_(None))
                .order_by(Demand.opened_at.desc().nulls_last(), Demand.title)
                .limit(500)
            )
        ).scalars()
    )
    on = today()
    picked: list[tuple[Any, eligibility.Reward | None]] = []
    for d in demands:
        facts = await eligibility.demand_facts(db, d.id)
        if facts is None:
            continue
        program = eligibility.program_for(programs, facts, referrer.kind, on)
        if program is None or eligibility.exclusion_reason(program, facts, referrer) is not None:
            continue
        picked.append((d, eligibility.reward_amount(program, facts)))
    names = await _client_names(db, [d.id for d, _ in picked])
    return [
        ReferrerOpeningOut(
            demand_id=d.id,
            title=d.title,
            location=d.location,
            employment_type=d.employment_type,
            work_mode=d.work_mode,
            client_name=names.get(d.id),
            reward_amount=r.amount if r else None,
            reward_currency=r.currency if r else None,
        )
        for d, r in picked
    ]


# ================================================================ upload channel
async def _assert_not_open(db: AsyncSession, demand_id: uuid.UUID | None, email: str) -> None:
    if demand_id is None:
        return
    from sqlalchemy import func

    found = (
        await db.execute(
            select(Referral.id).where(
                Referral.demand_id == demand_id,
                func.lower(Referral.referee_email) == email.casefold(),
                Referral.status.not_in(CLOSED_STATUSES),
            )
        )
    ).first()
    if found is not None:
        raise conflict("referral_exists", "This person was already referred for this opening")


async def prepare(
    db: AsyncSession, referrer: Referrer, demand_id: uuid.UUID | None, referee_email: str
) -> tuple[Any, eligibility.ProgramFacts | None]:
    """Shared referral-time checks: opening accepts referrals, self-referral (422), exclusions (409), duplicate
    open referral (409). Returns ``(demand, program)``."""
    demand = None
    if demand_id is not None:
        demand = await _demand_or_404(db, demand_id)
        if demand.status not in ACCEPTING_DEMAND_STATUSES:
            raise conflict(
                "demand_not_accepting", "This opening is not taking referrals", status=demand.status
            )
    eligibility.check_self_referral(referrer, referee_email)
    facts = await eligibility.demand_facts(db, demand_id)
    program = eligibility.program_for(await eligibility.load_programs(db), facts, referrer.kind, today())
    eligibility.check_exclusions(program, facts, referrer)
    await _assert_not_open(db, demand_id, referee_email)
    return demand, program


def new_referral(
    referral_id: uuid.UUID,
    tenant_id: uuid.UUID,
    human_id: str,
    referrer: Referrer,
    demand_id: uuid.UUID | None,
    program: eligibility.ProgramFacts | None,
    *,
    channel: str,
    name: str,
    email: str,
    phone: str | None,
    relationship: str | None = None,
    note: str | None = None,
    resume_file_id: uuid.UUID | None = None,
    share_link_id: uuid.UUID | None = None,
) -> Referral:
    return Referral(
        id=referral_id,
        tenant_id=tenant_id,
        human_id=human_id,
        program_id=program.id if program else None,
        demand_id=demand_id,
        **referrer.columns(),
        referee_name=name.strip()[:200],
        referee_email=email.strip(),
        referee_phone=(phone or "").strip()[:40] or None,
        relationship=(relationship or "").strip()[:100] or None,
        note=(note or "").strip() or None,
        resume_file_id=resume_file_id,
        channel=channel,
        share_link_id=share_link_id,
        status="submitted",
        reward_eligible=program is not None,
        ineligible_reason=None if program is not None else "no_program",
    )


async def flush_new(db: AsyncSession, referral: Referral) -> None:
    """Insert, mapping the partial UNIQUE (open referral per demand + email) to 409 ``referral_exists``."""
    try:
        async with db.begin_nested():
            db.add(referral)
            await db.flush()
    except IntegrityError as exc:
        if "uq_referrals_open_referee" in str(exc.orig):
            raise conflict("referral_exists", "This person was already referred for this opening") from exc
        raise


async def _send_referee_notice(
    db: AsyncSession, referral: Referral, referrer: Referrer, token: str, job_title: str | None
) -> None:
    from app.core.mailer import EmailMessage, send_email
    from app.services import identity_bridge

    tenant = await identity_bridge.get_tenant(db, referral.tenant_id)
    brand = tenant.name if tenant is not None else "our team"
    who = referrer.first_name or "A friend"
    role = job_title or "an opening"
    await send_email(
        EmailMessage(
            to=str(referral.referee_email),
            subject=f"{who} referred you to {brand} for {role}"[:200],
            text=(
                f"Hello {(referral.referee_name or '').split(' ')[0] or 'there'},\n\n"
                f"{who} referred you to {brand} for {role}.\n\n"
                "Before we look at your details we need your agreement. Read how we would use your data and "
                f"accept or decline the referral here:\n{secure_links.link_url(token)}\n\n"
                "If you decline, we delete your details and the resume you were referred with.\n\n"
                f"{brand}\n"
            ),
            tenant_id=referral.tenant_id,
            kind="referral_notice",
            from_name=brand,
        )
    )


async def refer(
    ctx: RequestContext,
    db: AsyncSession,
    referrer: Referrer,
    data: ReferIn,
    resume: bytes,
    filename: str | None,
) -> ReferrerReferralOut:
    """Upload channel: store the resume, create the ``submitted`` referral and send the referee the notice."""
    from app.services import files_service

    email = str(data.referee_email).strip()
    demand, program = await prepare(db, referrer, data.demand_id, email)
    settings = await get_settings_row(db)
    referral_id = uuid.uuid4()
    file = await files_service.store_upload(
        ctx, db, resume, filename, files_service.RESUME_KINDS, FILE_OWNER, referral_id
    )
    referral = new_referral(
        referral_id,
        referrer.tenant_id,
        await next_human_id(db, HUMAN_ID_KEY, HUMAN_ID_PREFIX, width=6),
        referrer,
        data.demand_id,
        program,
        channel="upload",
        name=data.referee_name,
        email=email,
        phone=data.referee_phone,
        relationship=data.relationship,
        note=data.note,
        resume_file_id=file.id,
    )
    await flush_new(db, referral)
    link, token = await secure_links.issue(
        db,
        tenant_id=referrer.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type=SUBJECT_REFERRAL,
        subject_id=referral.id,
        ttl=timedelta(days=settings.referee_link_days),
        max_uses=1,
        metadata={"channel": "upload"},
        created_by=referrer.id if referrer.kind == "user" else None,
    )
    referral.secure_link_id = link.id
    await db.flush()
    emit(db, status_event(referral, None))
    audit.record(
        "referral.submitted",
        "referral",
        referral.id,
        session=db,
        channel="upload",
        human_id=referral.human_id,
    )
    await _send_referee_notice(db, referral, referrer, token, demand.title if demand is not None else None)
    from app.services.referrals import tracking

    return (await tracking.referrer_view(db, [referral]))[0]


async def withdraw(db: AsyncSession, referrer: Referrer, referral_id: uuid.UUID) -> ReferrerReferralOut:
    """Only the referrer, only while ``submitted``: the referee data is minimised like a decline."""
    from app.services.referrals import acceptance, tracking

    referral = await db.get(Referral, referral_id, with_for_update=True)
    if referral is None or not tracking.owned_by(referral, referrer):
        raise not_found()
    if referral.status != "submitted":
        raise conflict(
            "referral_not_withdrawable", "Only a referral the referee has not answered can be withdrawn"
        )
    await acceptance.minimise(db, referral)
    set_status(db, referral, "withdrawn")
    if referral.secure_link_id is not None:
        await secure_links.revoke(db, referral.secure_link_id)
    await db.flush()
    return (await tracking.referrer_view(db, [referral]))[0]


# ================================================================ share channel
def _share_key(referrer: Referrer, demand_id: uuid.UUID | None) -> Any:
    who = (
        ReferralShareLink.referrer_user_id == referrer.id
        if referrer.kind == "user"
        else ReferralShareLink.referrer_candidate_id == referrer.id
    )
    dem = (
        ReferralShareLink.demand_id.is_(None)
        if demand_id is None
        else ReferralShareLink.demand_id == demand_id
    )
    return and_(
        ReferralShareLink.referrer_kind == referrer.kind, who, dem, ReferralShareLink.revoked_at.is_(None)
    )


async def share_link(
    db: AsyncSession, referrer: Referrer, demand_id: uuid.UUID | None
) -> ReferrerShareLinkOut:
    """The referrer's personal link for ``demand_id`` (general link when None). One active link per (referrer,
    demand): an unexpired one is returned again; an expired one is revoked and replaced."""
    await rate_limit.enforce("referral_share_link", f"{referrer.kind}:{referrer.id.hex}", 30, 3600)
    if demand_id is not None:
        d = await _demand_or_404(db, demand_id)
        if d.status not in ACCEPTING_DEMAND_STATUSES:
            raise conflict("demand_not_accepting", "This opening is not taking referrals", status=d.status)
        facts = await eligibility.demand_facts(db, demand_id)
        program = eligibility.program_for(await eligibility.load_programs(db), facts, referrer.kind, today())
        eligibility.check_exclusions(program, facts, referrer)
    existing = (
        await db.execute(select(ReferralShareLink).where(_share_key(referrer, demand_id)).with_for_update())
    ).scalar_one_or_none()
    if existing is not None and existing.expires_at > now():
        token = open_sealed(existing.tenant_id, existing.token_sealed)
        return ReferrerShareLinkOut(
            url=secure_links.link_url(token), demand_id=demand_id, expires_at=existing.expires_at
        )
    if existing is not None:
        existing.revoked_at = now()
        await secure_links.revoke(db, existing.secure_link_id)
        await db.flush()
    settings = await get_settings_row(db)
    row_id = uuid.uuid4()
    link, token = await secure_links.issue(
        db,
        tenant_id=referrer.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type=SUBJECT_SHARE,
        subject_id=row_id,
        ttl=timedelta(days=settings.share_link_days),
        max_uses=SHARE_LINK_MAX_USES,
        metadata={"channel": "share_link"},
        created_by=referrer.id if referrer.kind == "user" else None,
    )
    sealed, _, _ = seal(referrer.tenant_id, token)
    row = ReferralShareLink(
        id=row_id,
        tenant_id=referrer.tenant_id,
        **referrer.columns(),
        demand_id=demand_id,
        secure_link_id=link.id,
        token_sealed=sealed,
        expires_at=link.expires_at,
    )
    db.add(row)
    await db.flush()
    return ReferrerShareLinkOut(
        url=secure_links.link_url(token), demand_id=demand_id, expires_at=link.expires_at
    )


def referrer_of_share(row: ReferralShareLink) -> tuple[str, uuid.UUID]:
    return row.referrer_kind, (row.referrer_user_id or row.referrer_candidate_id)  # type: ignore[return-value]
