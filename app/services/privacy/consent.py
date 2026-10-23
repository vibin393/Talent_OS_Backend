"""Consent, notices and the AI-processing basis (Epic 12 story 12.1, FR-67).

* **Gateway checker** (``ai_consent_checker``), registered at startup through ``ai.set_consent_checker``:
  1. an explicitly withdrawn ``ai_processing`` consent (no newer grant) always denies;
  2. ``consent_required``: allow only with an active ``ai_processing`` consent;
  3. ``notice_based``: allow recruiter-sourced candidates (``NOTICE_BASED_SOURCES``) or active consent;
  4. calls without a candidate subject are allowed.
  The basis written to ``ai_actions.consent_basis`` is ``consent | notice | not_applicable | denied``.
* **Flag** ``candidates.ai_consent_state`` is written only here (through the candidates privacy port): on
  create (``initial_ai_consent_state``), grant/withdraw and basis recompute.
* **Withdrawal <= 1 h**: the gateway checks at call time (workers re-check when they execute), and
  ``ConsentWithdrawn(ai_processing)`` enqueues ``purge_ai_derived``; ``consent_withdrawal_sweep`` (every
  15 min) re-purges candidates that still have derived rows.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailMessage, send_email
from app.events.privacy import AIProcessingBasisChanged, ConsentGranted, ConsentWithdrawn
from app.models.privacy import CONSENT_PURPOSES, NOTICE_LOCALES, Consent, ConsentNotice
from app.schemas.privacy import (
    AIBasisOut,
    ConsentOut,
    ConsentRequestItemOut,
    ConsentRequestResult,
    NoticeCreate,
    NoticeOut,
    PublicConsentOut,
    PublicLinkOut,
)
from app.services import ai, settings_service
from app.services.candidates import privacy_port as candidates
from app.services.privacy import registry
from app.services.privacy.common import actor_id, now, require_tenant

AI_PURPOSE = "ai_processing"
# Recruiter-sourced candidates (spec: manual | import | api | sourced) in Epic 3's source vocabulary.
NOTICE_BASED_SOURCES: frozenset[str] = frozenset({"manual", "resume_upload", "bulk_import", "job_board", "referral"})
CONSENT_REQUEST_PURPOSE = "consent_request"
RECENT_REQUEST_WINDOW = timedelta(hours=72)
CONSENT_REQUEST_LIMIT_PER_MIN = 20

DEFAULT_NOTICES: dict[str, tuple[str, str]] = {
    "data_processing": (
        "Processing of your personal data",
        "We process the personal data in your profile to consider you for job opportunities.",
    ),
    "ai_processing": (
        "AI-assisted processing",
        "We may use AI tools to extract information from your resume, match you to roles and summarise "
        "screening answers. A person always makes the decisions.",
    ),
    "client_submission": (
        "Sharing your profile with a client",
        "With your consent we share your profile with the named client for the named role.",
    ),
    "marketing": ("Job alerts and updates", "We may contact you about other opportunities that match your profile."),
    # Epic 35 (FR-179): Phase 2 purposes (migration 20261005_222_01 seeds them for existing tenants).
    "bgv": (
        "Background verification",
        "With your consent we (or our verification partner) verify the education, employment, identity and other "
        "details you provide, as required for your assignment.",
    ),
    "reference_check": (
        "Reference checks",
        "With your consent we contact the referees you name to ask about your past work.",
    ),
    "vendor_collection": (
        "Profile shared by a supplier",
        "Your profile was shared with us by a staffing supplier who confirmed your consent. We use it to consider "
        "you for the role they submitted you to.",
    ),
    "whatsapp": (
        "WhatsApp messages",
        "With your consent we send you updates about your application or assignment on WhatsApp. You can stop at "
        "any time by replying STOP.",
    ),
    "engagement": (
        "Your engagement with us",
        "While you are on assignment we process your personal, bank and identity details to pay you, invoice our "
        "client and meet legal obligations. This is required while you are on assignment.",
    ),
}

# Epic 35 (A2): lawful basis per purpose. ``engagement`` is DPDP s.7 legitimate use (employment / engagement):
# recorded automatically on placement creation, never withdrawable while a placement is non-terminal, and never a
# reason to block payroll or invoicing. Every other purpose needs consent.
ENGAGEMENT_PURPOSE = "engagement"
PURPOSE_BASIS: dict[str, str] = {p: ("legitimate_use" if p == ENGAGEMENT_PURPOSE else "consent") for p in CONSENT_PURPOSES}
TERMINAL_PLACEMENT_STATUSES = ("ended", "dropped")


# ================================================================ basis
async def get_basis(db: AsyncSession) -> str:
    return await ai.processing_basis(db)


async def basis_out(ctx: RequestContext, db: AsyncSession) -> AIBasisOut:  # ruff: ignore[unused-function-argument]
    return AIBasisOut(
        basis=await get_basis(db),
        pending_count=await candidates.count_with_state(db, "pending"),
    )


async def set_basis(ctx: RequestContext, db: AsyncSession, basis: str) -> AIBasisOut:
    tenant_id = require_tenant(ctx)
    old = await get_basis(db)
    if old != basis:
        await ai.set_processing_basis(db, tenant_id, basis)
        audit.record("privacy.ai_basis_changed", "tenant", tenant_id, session=db, old=old, new=basis)
        emit(db, AIProcessingBasisChanged(tenant_id=tenant_id, basis=basis))
    return await basis_out(ctx, db)


# ================================================================ consent state
async def _ai_status(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """``granted`` when an active ai_processing consent exists, else ``withdrawn`` if one was withdrawn."""
    if not candidate_ids:
        return {}
    rows = (
        await db.execute(
            select(Consent.candidate_id, Consent.status).where(
                Consent.candidate_id.in_(candidate_ids), Consent.purpose == AI_PURPOSE
            )
        )
    ).all()
    out: dict[uuid.UUID, str] = {}
    for cid, status in rows:
        if status == "granted":
            out[cid] = "granted"
        elif out.get(cid) != "granted":
            out[cid] = "withdrawn"
    return out


def _state(status: str | None, basis: str, source: str | None, erased: bool) -> str:
    if erased:
        return "withdrawn"
    if status == "granted":
        return "granted"
    if status == "withdrawn":
        return "withdrawn"
    if basis == "notice_based" and source in NOTICE_BASED_SOURCES:
        return "not_required"
    return "pending"


async def initial_ai_consent_state(db: AsyncSession, source: str) -> str:
    """Flag for a candidate being created with ``source`` (no consents exist yet)."""
    return _state(None, await get_basis(db), source, erased=False)


async def recompute_ai_consent_state(db: AsyncSession, candidate_ids: list[uuid.UUID] | None = None) -> int:
    """Recompute the flag for ``candidate_ids`` (default: every candidate of the tenant)."""
    ids = candidate_ids if candidate_ids is not None else await candidates.all_ids(db, include_erased=True)
    basis = await get_basis(db)
    changed = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        facts = await candidates.basics(db, chunk)
        statuses = await _ai_status(db, chunk)
        states = {
            cid: _state(statuses.get(cid), basis, f["source"], f["erased_at"] is not None) for cid, f in facts.items()
        }
        changed += await candidates.set_ai_consent_state(db, states)
    return changed


async def ai_consent_checker(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    feature: str,  # ruff: ignore[unused-function-argument]
    subject: tuple[str, uuid.UUID] | None,
) -> ai.ConsentDecision:
    if subject is None or subject[0] != "candidate":
        return ai.ConsentDecision(True, "not_applicable")
    candidate_id = subject[1]
    status = (await _ai_status(db, [candidate_id])).get(candidate_id)
    if status == "granted":
        return ai.ConsentDecision(True, "consent")
    if status == "withdrawn":
        return ai.ConsentDecision(False, "denied")
    if await get_basis(db) == "notice_based":
        facts = (await candidates.basics(db, [candidate_id])).get(candidate_id)
        if facts is not None and facts["source"] in NOTICE_BASED_SOURCES and facts["erased_at"] is None:
            return ai.ConsentDecision(True, "notice")
    return ai.ConsentDecision(False, "denied")


def install() -> None:
    """Register the real consent checker with the AI gateway (idempotent; called at startup / worker boot)."""
    ai.set_consent_checker(ai_consent_checker, default=True)


# ================================================================ consents
async def has_active_consent(
    db: AsyncSession, candidate_id: uuid.UUID, purpose: str, client_id: uuid.UUID | None = None
) -> bool:
    stmt = select(Consent.id).where(
        Consent.candidate_id == candidate_id, Consent.purpose == purpose, Consent.status == "granted"
    )
    if client_id is not None:
        stmt = stmt.where(Consent.client_id == client_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def list_consents(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[ConsentOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(Consent).where(Consent.candidate_id == candidate_id).order_by(Consent.created_at.desc())
        )
    ).scalars()
    return [ConsentOut.model_validate(r) for r in rows]


def _validate_purpose(purpose: str, client_id: uuid.UUID | None) -> None:
    if purpose not in CONSENT_PURPOSES:
        raise ValidationFailed("Unknown consent purpose", details={"fields": {"purpose": "Unknown purpose"}})
    if purpose == "client_submission" and client_id is None:
        raise ValidationFailed(
            "client_id is required", details={"fields": {"client_id": "Required for client_submission"}}
        )


async def grant_consent(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    purpose: str,
    *,
    source: str,
    client_id: uuid.UUID | None = None,
    notice_id: uuid.UUID | None = None,
    evidence: dict[str, Any] | None = None,
) -> Consent:
    tenant_id = require_tenant(ctx)
    _validate_purpose(purpose, client_id)
    if notice_id is None:
        notice = await active_notice(db, tenant_id, purpose)
        notice_id = notice.id if notice is not None else None
    row = Consent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        purpose=purpose,
        client_id=client_id,
        status="granted",
        source=source,
        notice_id=notice_id,
        granted_at=now(),
        evidence=evidence,
    )
    db.add(row)
    await db.flush()
    audit.record("consent.granted", "candidate", candidate_id, session=db, purpose=purpose, source=source)
    if purpose == AI_PURPOSE:
        await recompute_ai_consent_state(db, [candidate_id])
    emit(db, ConsentGranted(tenant_id=tenant_id, candidate_id=candidate_id, purpose=purpose, consent_id=row.id))
    return row


async def withdraw_consent(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    purpose: str,
    *,
    by_kind: str = "user",
    client_id: uuid.UUID | None = None,
) -> int:
    """Withdraw every active consent of ``candidate_id`` for ``purpose``. Returns the number withdrawn."""
    tenant_id = require_tenant(ctx)
    if purpose == ENGAGEMENT_PURPOSE and await has_active_engagement(db, candidate_id):
        raise ConflictError(
            "Engagement processing is required while the worker is on assignment",
            code="engagement_active",
            details={"purpose": purpose},
        )
    stmt = select(Consent).where(
        Consent.candidate_id == candidate_id, Consent.purpose == purpose, Consent.status == "granted"
    )
    if client_id is not None:
        stmt = stmt.where(Consent.client_id == client_id)
    rows = list((await db.execute(stmt)).scalars())
    for row in rows:
        row.status = "withdrawn"
        row.withdrawn_at = now()
        row.withdrawn_by_kind = by_kind
    if purpose == AI_PURPOSE and not rows:
        # Record an explicit withdrawal even without a prior grant: the gateway must deny from now on.
        db.add(
            Consent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                purpose=purpose,
                status="withdrawn",
                source="candidate_link" if by_kind == "candidate" else "recruiter_attestation",
                withdrawn_at=now(),
                withdrawn_by_kind=by_kind,
            )
        )
    await db.flush()
    audit.record("consent.withdrawn", "candidate", candidate_id, session=db, purpose=purpose, by=by_kind)
    if purpose == AI_PURPOSE:
        await recompute_ai_consent_state(db, [candidate_id])
    emit(db, ConsentWithdrawn(tenant_id=tenant_id, candidate_id=candidate_id, purpose=purpose))
    return len(rows)


async def withdraw_by_id(ctx: RequestContext, db: AsyncSession, consent_id: uuid.UUID) -> ConsentOut:
    row = await db.get(Consent, consent_id)
    if row is None:
        raise NotFound("Consent not found")
    if row.status != "granted":
        raise ConflictError("Consent is not active", code="consent_not_active")
    await withdraw_consent(ctx, db, row.candidate_id, row.purpose, client_id=row.client_id)
    await db.refresh(row)
    return ConsentOut.model_validate(row)


async def granted_purposes(db: AsyncSession, candidate_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(Consent.purpose).where(Consent.candidate_id == candidate_id, Consent.status == "granted").distinct()
    )
    return sorted(rows.scalars())


# ================================================================ consent requests (secure links)
async def _recent_request(db: AsyncSession, candidate_id: uuid.UUID, purpose: str) -> bool:
    links = await secure_links.links_for_subject(db, "candidate", candidate_id)
    cutoff = now() - RECENT_REQUEST_WINDOW
    return any(
        link.purpose == CONSENT_REQUEST_PURPOSE
        and link.created_at >= cutoff
        and link.revoked_at is None
        and (link.meta or {}).get("purpose") == purpose
        for link in links
    )


async def request_consents(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_ids: list[uuid.UUID],
    purpose: str,
    client_id: uuid.UUID | None = None,
) -> ConsentRequestResult:
    tenant_id = require_tenant(ctx)
    user = actor_id(ctx)
    await rate_limit.enforce("consent_request", str(user), CONSENT_REQUEST_LIMIT_PER_MIN, 60)
    _validate_purpose(purpose, client_id)
    ids = list(dict.fromkeys(candidate_ids))
    facts = await candidates.basics(db, ids)
    ttl = await settings_service.link_ttl(db, CONSENT_REQUEST_PURPOSE)
    items: list[ConsentRequestItemOut] = []
    outbox: list[EmailMessage] = []
    for cid in ids:
        info = facts.get(cid)
        if info is None or info["erased_at"] is not None:
            items.append(ConsentRequestItemOut(candidate_id=cid, status="not_found"))
            continue
        if await has_active_consent(db, cid, purpose, client_id):
            items.append(ConsentRequestItemOut(candidate_id=cid, status="skipped_already_granted"))
            continue
        if not info["primary_email"]:
            items.append(ConsentRequestItemOut(candidate_id=cid, status="skipped_no_email"))
            continue
        if await _recent_request(db, cid, purpose):
            items.append(ConsentRequestItemOut(candidate_id=cid, status="skipped_recent"))
            continue
        link, token = await secure_links.issue(
            db,
            tenant_id=tenant_id,
            purpose=CONSENT_REQUEST_PURPOSE,
            subject_type="candidate",
            subject_id=cid,
            ttl=ttl,
            metadata={"purpose": purpose, "client_id": str(client_id) if client_id else None},
            created_by=user,
        )
        audit.record("consent.requested", "candidate", cid, session=db, purpose=purpose, link_id=link.id)
        title = DEFAULT_NOTICES.get(purpose, ("Consent request", ""))[0]
        outbox.append(
            EmailMessage(
                to=info["primary_email"],
                subject=f"Your consent is requested: {title}",
                text=(
                    "Hello,\n\nWe would like your consent for the following use of your personal data: "
                    f"{title}.\n\nReview the notice and answer here:\n{secure_links.link_url(token)}\n\n"
                    f"This link expires in {ttl.days} days.\n"
                ),
                tenant_id=tenant_id,
                kind="consent_request",
            )
        )
        items.append(ConsentRequestItemOut(candidate_id=cid, status="sent"))
    await db.flush()
    for message in outbox:
        await send_email(message)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return ConsentRequestResult(items=items, counts=counts)


async def view_link(token: str) -> PublicLinkOut:
    link = await secure_links.resolve(token, ("consent_request",))
    from app.core.database import tenant_session

    async with tenant_session(link.tenant_id) as db:
        purpose = str(link.meta.get("purpose") or AI_PURPOSE)
        notice = await active_notice(db, link.tenant_id, purpose)
        from app.services import identity_bridge

        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        return PublicLinkOut(
            purpose=link.purpose,
            tenant_name=tenant.name if tenant is not None else None,
            expires_at=link.expires_at,
            notice=NoticeOut.model_validate(notice) if notice is not None else None,
        )


async def respond_link(
    token: str, granted: bool, notice_id: uuid.UUID | None, *, ip_hash: str | None, user_agent: str | None
) -> PublicConsentOut:
    link = await secure_links.resolve(token, ("consent_request",))
    from app.core.database import tenant_session

    ctx = RequestContext(tenant_id=link.tenant_id)
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "candidate", link.subject_id)
        await secure_links.consume(db, link.id)
        purpose = str(link.meta.get("purpose") or AI_PURPOSE)
        client_raw = link.meta.get("client_id")
        client_id = uuid.UUID(client_raw) if client_raw else None
        if granted:
            await grant_consent(
                ctx,
                db,
                link.subject_id,
                purpose,
                source="candidate_link",
                client_id=client_id,
                notice_id=notice_id,
                evidence={"ip_hash": ip_hash, "user_agent": (user_agent or "")[:300], "link_id": str(link.id)},
            )
        else:
            audit.record("consent.declined", "candidate", link.subject_id, session=db, purpose=purpose)
    return PublicConsentOut(status="granted" if granted else "declined")


# ================================================================ notices
def register_default_notice(purpose: str, title: str, body_md: str) -> None:
    """Epic 36 (AD-39): an owner domain registers the default ``en`` notice of its consent purpose from its
    ``hooks.py``. Tenants are seeded lazily by ``ensure_default_notices`` (existing notices are never changed);
    re-registering replaces the default text. Purposes outside ``CONSENT_PURPOSES`` -> 422 ``unknown_purpose``."""
    if purpose not in CONSENT_PURPOSES:
        raise ValidationFailed(
            f"Unknown consent purpose: {purpose}",
            code="unknown_purpose",
            details={"fields": [{"field": "purpose", "message": "Unknown consent purpose"}]},
        )
    if not title.strip() or not body_md.strip():
        raise ValidationFailed("A notice needs a title and a body", code="invalid_notice")
    DEFAULT_NOTICES[purpose] = (title, body_md)


async def ensure_default_notices(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    existing = set((await db.execute(select(ConsentNotice.purpose).where(ConsentNotice.locale == "en"))).scalars())
    for purpose, (title, body) in DEFAULT_NOTICES.items():
        if purpose not in existing:
            db.add(
                ConsentNotice(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    purpose=purpose,
                    locale="en",
                    version=1,
                    title=title,
                    body_md=body,
                    active=True,
                )
            )
    await db.flush()


async def active_notice(
    db: AsyncSession, tenant_id: uuid.UUID, purpose: str, locale: str = "en"
) -> ConsentNotice | None:
    stmt = select(ConsentNotice).where(
        ConsentNotice.purpose == purpose, ConsentNotice.locale == locale, ConsentNotice.active.is_(True)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None and locale == "en":
        await ensure_default_notices(db, tenant_id)
        row = (await db.execute(stmt)).scalar_one_or_none()
    return row


async def list_notices(ctx: RequestContext, db: AsyncSession, purpose: str | None = None) -> list[NoticeOut]:
    await ensure_default_notices(db, require_tenant(ctx))
    stmt = select(ConsentNotice)
    if purpose:
        stmt = stmt.where(ConsentNotice.purpose == purpose)
    rows = (
        await db.execute(stmt.order_by(ConsentNotice.purpose, ConsentNotice.locale, ConsentNotice.version.desc()))
    ).scalars()
    return [NoticeOut.model_validate(r) for r in rows]


async def create_notice(ctx: RequestContext, db: AsyncSession, data: NoticeCreate) -> NoticeOut:
    tenant_id = require_tenant(ctx)
    if data.locale not in NOTICE_LOCALES:
        raise ValidationFailed("Unsupported locale", details={"fields": {"locale": "Unsupported locale"}})
    await ensure_default_notices(db, tenant_id)
    latest = (
        await db.execute(
            select(func.max(ConsentNotice.version)).where(
                ConsentNotice.purpose == data.purpose, ConsentNotice.locale == data.locale
            )
        )
    ).scalar_one()
    row = ConsentNotice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        purpose=data.purpose,
        locale=data.locale,
        version=int(latest or 0) + 1,
        title=data.title,
        body_md=data.body_md,
        active=False,
        created_by=actor_id(ctx),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return NoticeOut.model_validate(row)


async def activate_notice(ctx: RequestContext, db: AsyncSession, notice_id: uuid.UUID) -> NoticeOut:  # ruff: ignore[unused-function-argument]
    row = await db.get(ConsentNotice, notice_id)
    if row is None:
        raise NotFound("Notice not found")
    if not row.active:
        await db.execute(
            update(ConsentNotice)
            .where(
                ConsentNotice.purpose == row.purpose,
                ConsentNotice.locale == row.locale,
                ConsentNotice.active.is_(True),
            )
            .values(active=False)
        )
        await db.flush()
        row.active = True
        await db.flush()
        audit.record("consent_notice.activated", "consent_notice", row.id, session=db, version=row.version)
    await db.refresh(row)
    return NoticeOut.model_validate(row)


# ================================================================ AI-derived data (withdrawal)
async def purge_ai_derived(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, int]:
    """Delete AI-derived rows of a candidate whose AI consent is withdrawn (idempotent)."""
    counts: dict[str, int] = {}
    for name, purge, _ in registry.ai_derived_purgers():
        for key, value in (await purge(db, candidate_id)).items():
            counts[f"{name}.{key}"] = value
    return counts


async def withdrawn_with_derived(db: AsyncSession) -> set[uuid.UUID]:
    withdrawn = await candidates.ids_with_state(db, "withdrawn")
    if not withdrawn:
        return set()
    found: set[uuid.UUID] = set()
    for _, _, find in registry.ai_derived_purgers():
        found |= await find(db, withdrawn)
    return found


async def list_candidate_consents(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[ConsentOut]:
    if not await candidates.exists(db, candidate_id):
        raise NotFound("Candidate not found")
    return await list_consents(ctx, db, candidate_id)


# ================================================================ Epic 35 (A2): public service functions
async def has_active_engagement(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    """True while the candidate has a non-terminal placement (table read through metadata, no domain import)."""
    from app.core.models import Base

    placements = Base.metadata.tables.get("placements")
    if placements is None:
        return False
    row = (
        await db.execute(
            select(placements.c.id)
            .where(
                placements.c.candidate_id == candidate_id,
                placements.c.status.not_in(TERMINAL_PLACEMENT_STATUSES),
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def require(db: AsyncSession, candidate_id: uuid.UUID, purpose: str) -> Consent:
    """The active consent of ``candidate_id`` for ``purpose``, else 409 ``consent_required``.

    Epic 20 calls it before starting BGV / a reference check (FR-111/114), Epic 33 before WhatsApp."""
    if purpose not in CONSENT_PURPOSES:
        raise ValidationFailed("Unknown consent purpose", details={"fields": {"purpose": "Unknown purpose"}})
    row = (
        await db.execute(
            select(Consent)
            .where(Consent.candidate_id == candidate_id, Consent.purpose == purpose, Consent.status == "granted")
            .order_by(Consent.granted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ConflictError(
            "The candidate has not consented to this processing",
            code="consent_required",
            details={"purpose": purpose},
        )
    return row


async def record(
    db: AsyncSession,
    candidate_id: uuid.UUID,
    purpose: str,
    source: str,
    notice_version: int | None,
    actor: Any,
    *,
    evidence: dict[str, Any] | None = None,
) -> Consent:
    """Record a grant (Vendor attestation FR-95, WhatsApp opt-in, Worker portal, placement). Idempotent: an active
    grant for the purpose is returned as is. ``actor`` is a ``RequestContext`` or ``None`` (system)."""
    from app.models.privacy import CONSENT_SOURCES

    if source not in CONSENT_SOURCES:
        raise ValidationFailed("Unknown consent source", details={"fields": {"source": "Unknown source"}})
    existing = (
        await db.execute(
            select(Consent)
            .where(Consent.candidate_id == candidate_id, Consent.purpose == purpose, Consent.status == "granted")
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    notice_id = None
    if notice_version is not None:
        notice_id = (
            await db.execute(
                select(ConsentNotice.id).where(
                    ConsentNotice.purpose == purpose, ConsentNotice.locale == "en", ConsentNotice.version == notice_version
                )
            )
        ).scalar_one_or_none()
    ctx = actor if isinstance(actor, RequestContext) else RequestContext(tenant_id=tenant_id)
    return await grant_consent(ctx, db, candidate_id, purpose, source=source, notice_id=notice_id, evidence=evidence)


async def record_engagement(db: AsyncSession, candidate_id: uuid.UUID, *, source: str = "placement") -> Consent:
    """``PlacementCreated`` handler body (and go-live import): engagement basis, current notice version."""
    return await record(db, candidate_id, ENGAGEMENT_PURPOSE, source, None, None)
