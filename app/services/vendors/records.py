"""Vendors, Vendor Status and vendor documents (Epic 17 A2, FR-93, §5.4).

* ``transition_vendor`` is the only writer of ``vendors.status`` (AD-4 table below); it audits, pauses /
  resumes
  releases and emits ``VendorStatusChanged`` (Epic 14's handler ends the vendor's sessions).
* Vendor documents are Epic 22 ``documents`` rows (``owner_kind='vendor'``); each is granted to the vendor org
  (``org_grants``) so the Vendor Portal sees it. Validity (``valid|expiring|expired``) is derived from
  ``expires_on``, never stored.
* Vendor Agreement gate: an active Epic 16 Contract ``vendor_agreement``; otherwise an unexpired ``agreement``
  document satisfies the gate (feature fallback, logged).
"""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, org_grants
from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.encrypted import blind_index, set_identity_field, unmask
from app.core.errors import Forbidden
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.vendors import VendorStatusChanged
from app.models.documents import Document
from app.models.vendors import (
    OPEN_RELEASE_STATUSES,
    Vendor,
    VendorBankAccount,
    VendorDocumentRequirement,
    VendorDocumentUpload,
    VendorGstin,
    VendorRelease,
)
from app.schemas.vendors import (
    BankAccountIn,
    BankAccountOut,
    GstinIn,
    GstinOut,
    VendorCreate,
    VendorDetailOut,
    VendorDocumentOut,
    VendorOut,
    VendorSettingsOut,
    VendorSettingsUpdate,
    VendorSummary,
    VendorUpdate,
)
from app.services.vendors.common import (
    conflict,
    get_vendor,
    invalid,
    load_settings,
    not_found,
    now,
    tenant_of,
    user_id,
)

ENTITY = "vendor"
OWNER_KIND = "vendor"
EXPIRING_DAYS = 30
BANK_SUBJECT = "vendor_bank_change"

# (from, to) -> allowed actor kinds; ``None`` from = create.
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "prospective"): frozenset({"user"}),
    ("prospective", "onboarding"): frozenset({"user"}),
    ("onboarding", "active"): frozenset({"user"}),
    ("active", "suspended"): frozenset({"user", "system"}),
    ("suspended", "active"): frozenset({"user"}),
    ("active", "inactive"): frozenset({"user"}),
    ("suspended", "inactive"): frozenset({"user"}),
}
REASON_REQUIRED = frozenset({"suspended", "inactive"})
GUARDED = frozenset({"active"})


# ================================================================ read models
def vendor_out(v: Vendor) -> VendorOut:
    return VendorOut(
        id=v.id,
        human_id=v.human_id,
        legal_name=v.legal_name,
        status=v.status,
        tier=v.tier,
        pan_last4=v.pan_last4,
        address=v.address or {},
        primary_contact=v.primary_contact or {},
        services=list(v.services or []),
        skill_ids=list(v.skill_ids or []),
        regions=list(v.regions or []),
        owner_id=v.owner_id,
        suspended_reason=v.suspended_reason,
        inactive_reason=v.inactive_reason,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def bank_out(b: VendorBankAccount) -> BankAccountOut:
    return BankAccountOut(
        id=b.id,
        account_name=b.account_name,
        account_no_last4=b.account_no_last4,
        ifsc=b.ifsc,
        is_primary=b.is_primary,
        status=b.status,
        approval_request_id=b.approval_request_id,
    )


async def detail_out(db: AsyncSession, v: Vendor) -> VendorDetailOut:
    gstins = (
        await db.execute(
            select(VendorGstin).where(VendorGstin.vendor_id == v.id).order_by(VendorGstin.created_at)
        )
    ).scalars()
    banks = (
        await db.execute(
            select(VendorBankAccount)
            .where(VendorBankAccount.vendor_id == v.id)
            .order_by(VendorBankAccount.created_at)
        )
    ).scalars()
    blockers = await activation_blockers(db, v)
    return VendorDetailOut(
        **vendor_out(v).model_dump(),
        gstins=[GstinOut(id=g.id, gstin=g.gstin, state_code=g.state_code) for g in gstins],
        bank_accounts=[bank_out(b) for b in banks],
        activation_blockers=blockers,
        agreement_active="agreement" not in blockers,
    )


def summary(v: Vendor) -> VendorSummary:
    return VendorSummary(id=v.id, human_id=v.human_id, legal_name=v.legal_name, status=v.status)


# ================================================================ CRUD
async def list_vendors(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    status: str | None = None,
    tier: str | None = None,
    q: str | None = None,
) -> list[VendorOut]:
    stmt = select(Vendor).where(Vendor.archived_at.is_(None))
    if status:
        stmt = stmt.where(Vendor.status == status)
    if tier:
        stmt = stmt.where(Vendor.tier == tier)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(Vendor.legal_name.ilike(term), Vendor.human_id.ilike(term)))
    rows = (await db.execute(stmt.order_by(Vendor.legal_name, Vendor.id).limit(500))).scalars()
    return [vendor_out(v) for v in rows]


def _set_contact(v: Vendor, contact: Any) -> None:
    v.primary_contact = {k: val for k, val in contact.model_dump().items() if val}


async def create_vendor(ctx: RequestContext, db: AsyncSession, data: VendorCreate) -> VendorDetailOut:
    tenant_id = tenant_of(db)
    if data.pan:
        await _check_pan_unique(db, tenant_id, data.pan, None)
    v = Vendor(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "VEN", width=6),
        legal_name=data.legal_name,
        status="prospective",
        address=data.address,
        services=list(data.services),
        skill_ids=list(data.skill_ids),
        regions=list(data.regions),
        tier=data.tier,
        owner_id=data.owner_id or user_id(ctx),
    )
    _set_contact(v, data.primary_contact)
    if data.pan:
        set_identity_field(v, "pan", data.pan.upper(), tenant_id)
    db.add(v)
    await db.flush()
    await db.refresh(v)
    return await detail_out(db, v)


async def _check_pan_unique(
    db: AsyncSession, tenant_id: uuid.UUID, pan: str, vendor_id: uuid.UUID | None
) -> None:
    stmt = select(Vendor.id).where(Vendor.pan_bidx == blind_index(tenant_id, pan.upper()))
    if vendor_id is not None:
        stmt = stmt.where(Vendor.id != vendor_id)
    if (await db.execute(stmt.limit(1))).first() is not None:
        raise conflict("vendor_pan_exists", "Another vendor has this PAN")


async def get_vendor_detail(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID) -> VendorDetailOut:  # ruff: ignore[unused-function-argument]
    return await detail_out(db, await get_vendor(db, vendor_id))


async def update_vendor(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    data: VendorUpdate,
) -> VendorDetailOut:
    v = await get_vendor(db, vendor_id, lock=True)
    fields = data.model_dump(exclude_unset=True)
    if "pan" in fields:
        pan = fields.pop("pan")
        if pan:
            await _check_pan_unique(db, v.tenant_id, pan, v.id)
        set_identity_field(v, "pan", pan.upper() if pan else None, v.tenant_id)
    if "primary_contact" in fields:
        fields.pop("primary_contact")
        if data.primary_contact is not None:
            _set_contact(v, data.primary_contact)
    for key, value in fields.items():
        if key in {"legal_name"} and value is None:
            continue
        setattr(v, key, list(value) if isinstance(value, list) else value)
    await db.flush()
    await db.refresh(v)
    return await detail_out(db, v)


# ================================================================ status machine
async def agreement_active(db: AsyncSession, vendor_id: uuid.UUID, *, on: date | None = None) -> bool:
    from app.models.commercials import Contract

    found = (
        await db.execute(
            select(Contract.id)
            .where(
                Contract.vendor_id == vendor_id,
                Contract.contract_type == "vendor_agreement",
                Contract.status == "active",
            )
            .limit(1)
        )
    ).first()
    if found is not None:
        return True
    today = on or now().date()
    doc = (
        await db.execute(
            select(Document.id)
            .where(
                Document.owner_kind == OWNER_KIND,
                Document.owner_id == vendor_id,
                Document.category == "agreement",
                Document.archived_at.is_(None),
                or_(Document.expires_on.is_(None), Document.expires_on >= today),
            )
            .limit(1)
        )
    ).first()
    if doc is not None:
        logger.info(
            "Vendor {} agreement gate satisfied by an agreement document (no active contract)", vendor_id
        )
        return True
    return False


async def mandatory_categories(db: AsyncSession) -> list[str]:
    settings = await load_settings(db)
    cats = set(settings.mandatory_document_kinds or [])
    rows = (await db.execute(select(VendorDocumentRequirement))).scalars()
    for r in rows:
        if r.is_mandatory:
            cats.add(r.category)
        else:
            cats.discard(r.category)
    return sorted(cats)


def doc_status(expires_on: date | None, today: date) -> str:
    if expires_on is None:
        return "no_expiry"
    if expires_on < today:
        return "expired"
    if expires_on <= today + timedelta(days=EXPIRING_DAYS):
        return "expiring"
    return "valid"


async def _live_docs(db: AsyncSession, vendor_id: uuid.UUID) -> list[Document]:
    return list(
        (
            await db.execute(
                select(Document).where(
                    Document.owner_kind == OWNER_KIND,
                    Document.owner_id == vendor_id,
                    Document.archived_at.is_(None),
                )
            )
        ).scalars()
    )


async def activation_blockers(db: AsyncSession, v: Vendor) -> list[str]:
    today = now().date()
    missing: list[str] = []
    if not await agreement_active(db, v.id, on=today):
        missing.append("agreement")
    docs = await _live_docs(db, v.id)
    for cat in await mandatory_categories(db):
        if not any(d.category == cat and doc_status(d.expires_on, today) != "expired" for d in docs):
            missing.append(f"document:{cat}")
    return missing


async def transition_vendor(
    ctx: RequestContext,
    db: AsyncSession,
    vendor_id: uuid.UUID,
    to: str,
    actor: ActorRef | None = None,
    reason: str | None = None,
) -> VendorDetailOut:
    actor = actor or actor_from_ctx(ctx)
    v = await get_vendor(db, vendor_id, lock=True)
    frm = v.status
    allowed = TRANSITIONS.get((frm, to))
    if allowed is None:
        raise conflict(
            "transition_not_allowed",
            f"A vendor cannot move from {frm} to {to}",
            from_status=frm,
            to_status=to,
        )
    if actor.kind not in allowed:
        raise conflict("transition_not_allowed", "This actor cannot make this change", actor=actor.kind)
    if to in REASON_REQUIRED and not (reason or "").strip():
        raise invalid("reason_required", "A reason is required")
    if to in GUARDED:
        missing = await activation_blockers(db, v)
        if missing:
            raise conflict("vendor_activation_blocked", "The vendor cannot be activated yet", missing=missing)
    v.status = to
    if to == "suspended":
        v.suspended_reason = reason
    elif to == "inactive":
        v.inactive_reason = reason
    elif to == "active":
        v.suspended_reason = None
    await db.flush()
    audit.record(
        "vendor.status_changed",
        ENTITY,
        v.id,
        session=db,
        from_status=frm,
        to_status=to,
        actor_kind=actor.kind,
    )
    await _cascade_releases(db, v.id, to)
    emit(db, VendorStatusChanged(tenant_id=v.tenant_id, vendor_id=v.id, from_status=frm, to_status=to))
    await db.refresh(v)
    return await detail_out(db, v)


async def _cascade_releases(db: AsyncSession, vendor_id: uuid.UUID, to: str) -> None:
    """Suspension / inactivation pauses active releases (``paused_by_system``); re-activation resumes them."""
    if to in {"suspended", "inactive"}:
        await db.execute(
            update(VendorRelease)
            .where(VendorRelease.vendor_id == vendor_id, VendorRelease.status == "active")
            .values(status="paused", paused_by_system=True)
            .execution_options(synchronize_session=False)
        )
    elif to == "active":
        await db.execute(
            update(VendorRelease)
            .where(
                VendorRelease.vendor_id == vendor_id,
                VendorRelease.status == "paused",
                VendorRelease.paused_by_system.is_(True),
            )
            .values(status="active", paused_by_system=False)
            .execution_options(synchronize_session=False)
        )


# ================================================================ GSTINs, bank accounts, reveal
async def add_gstin(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID, data: GstinIn) -> GstinOut:  # ruff: ignore[unused-function-argument]
    from app.services import legal_entities_service

    v = await get_vendor(db, vendor_id)
    pan = None
    if v.pan is not None:
        from app.core.encrypted import open_sealed

        pan = open_sealed(v.tenant_id, v.pan)
    errors = legal_entities_service.validate_gstin(data.gstin, state_code=data.state_code, pan=pan)
    if errors:
        raise invalid("gstin_invalid", "The GSTIN is not valid", errors=errors)
    exists = (
        await db.execute(
            select(VendorGstin.id).where(VendorGstin.vendor_id == v.id, VendorGstin.gstin == data.gstin)
        )
    ).first()
    if exists is not None:
        raise conflict("gstin_exists", "This GSTIN is already recorded")
    row = VendorGstin(
        id=uuid.uuid4(), tenant_id=v.tenant_id, vendor_id=v.id, gstin=data.gstin, state_code=data.state_code
    )
    db.add(row)
    await db.flush()
    return GstinOut(id=row.id, gstin=row.gstin, state_code=row.state_code)


async def delete_gstin(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    gstin_id: uuid.UUID,
) -> None:
    row = (
        await db.execute(
            select(VendorGstin).where(VendorGstin.id == gstin_id, VendorGstin.vendor_id == vendor_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("GSTIN")
    await db.delete(row)
    await db.flush()


async def request_bank_account(
    ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID, data: BankAccountIn
) -> BankAccountOut:
    """Request a bank account change (pending; approval subject ``vendor_bank_change``)."""
    from app.services import approvals

    v = await get_vendor(db, vendor_id)
    row = VendorBankAccount(
        id=uuid.uuid4(),
        tenant_id=v.tenant_id,
        vendor_id=v.id,
        account_name=data.account_name,
        ifsc=data.ifsc,
        is_primary=data.is_primary,
        status="pending",
        requested_by=user_id(ctx),
    )
    set_identity_field(row, "account_no", data.account_no, v.tenant_id)
    db.add(row)
    await db.flush()
    result = await approvals.start(
        ctx, db, BANK_SUBJECT, row.id, {"vendor_id": str(v.id), "is_primary": data.is_primary}
    )
    row.approval_request_id = result.request_id
    if result.status == "auto_approved":
        await _activate_bank(db, row)
    await db.flush()
    return bank_out(row)


async def _activate_bank(db: AsyncSession, row: VendorBankAccount) -> None:
    if row.is_primary:
        await db.execute(
            update(VendorBankAccount)
            .where(
                VendorBankAccount.vendor_id == row.vendor_id,
                VendorBankAccount.id != row.id,
                VendorBankAccount.status == "active",
            )
            .values(is_primary=False)
            .execution_options(synchronize_session=False)
        )
    row.status = "active"
    await db.flush()


async def on_bank_change_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # ruff: ignore[unused-function-argument]
    row = (
        await db.execute(
            select(VendorBankAccount).where(VendorBankAccount.id == decision.subject_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != "pending":
        return
    if decision.outcome in ("approved", "auto_approved"):
        await _activate_bank(db, row)
    else:
        row.status = "rejected"
        await db.flush()


async def describe_bank_changes(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, Any]:
    from app.schemas.approvals import SubjectSummary

    rows = (
        await db.execute(
            select(VendorBankAccount, Vendor.legal_name)
            .join(Vendor, Vendor.id == VendorBankAccount.vendor_id)
            .where(VendorBankAccount.id.in_(ids))
        )
    ).all()
    return {
        b.id: SubjectSummary(label=f"Bank ****{b.account_no_last4}", title=f"{name}: bank account change")
        for b, name in rows
    }


async def reveal(
    ctx: RequestContext,
    db: AsyncSession,
    vendor_id: uuid.UUID,
    field: str,
    bank_account_id: uuid.UUID | None = None,
) -> str:
    v = await get_vendor(db, vendor_id)
    if field == "pan":
        if v.pan is None:
            raise not_found("PAN")
        return await unmask(ctx, db, v, "pan")
    if bank_account_id is None:
        raise invalid("bank_account_required", "Choose the bank account to reveal")
    row = (
        await db.execute(
            select(VendorBankAccount).where(
                VendorBankAccount.id == bank_account_id, VendorBankAccount.vendor_id == v.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("Bank account")
    try:
        return await unmask(ctx, db, row, "account_no")
    except Forbidden:
        raise
    except ValueError as exc:  # pragma: no cover - empty value
        raise not_found("Bank account") from exc


# ================================================================ documents
async def document_outs(db: AsyncSession, vendor_id: uuid.UUID) -> list[VendorDocumentOut]:
    today = now().date()
    docs = sorted(await _live_docs(db, vendor_id), key=lambda d: d.created_at, reverse=True)
    pending = set(
        (
            await db.execute(
                select(VendorDocumentUpload.document_id).where(
                    VendorDocumentUpload.vendor_id == vendor_id,
                    VendorDocumentUpload.status == "pending_review",
                )
            )
        ).scalars()
    )
    return [
        VendorDocumentOut(
            id=d.id,
            human_id=d.human_id,
            category=d.category,
            title=d.title,
            expires_on=d.expires_on,
            status=doc_status(d.expires_on, today),
            current_version_id=d.current_version_id,
            pending_review=d.id in pending,
            created_at=d.created_at,
        )
        for d in docs
    ]


async def list_documents(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
) -> list[VendorDocumentOut]:
    await get_vendor(db, vendor_id)
    return await document_outs(db, vendor_id)


async def upload_document(
    ctx: RequestContext,
    db: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    data: bytes,
    filename: str | None,
    category: str,
    title: str | None,
    expires_on: date | None,
) -> VendorDocumentOut:
    from app.services.documents import repository

    v = await get_vendor(db, vendor_id)
    doc = await repository.upload_document(
        ctx,
        db,
        owner_kind=OWNER_KIND,
        owner_id=v.id,
        data=data,
        filename=filename,
        category=category,
        title=title,
        expires_on=expires_on,
    )
    await org_grants.grant(db, "vendor", v.id, "document", doc.id, actor_from_ctx(ctx))
    outs = await document_outs(db, v.id)
    return next(o for o in outs if o.id == doc.id)


async def mark_upload_reviewed(
    ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    await db.execute(
        update(VendorDocumentUpload)
        .where(
            VendorDocumentUpload.vendor_id == vendor_id,
            VendorDocumentUpload.document_id == document_id,
            VendorDocumentUpload.status == "pending_review",
        )
        .values(status="reviewed", reviewed_by=user_id(ctx), reviewed_at=now())
        .execution_options(synchronize_session=False)
    )


# ================================================================ expiry job (daily, queue maintenance)
async def check_documents(
    ctx: RequestContext, db: AsyncSession, *, today: date | None = None
) -> dict[str, Any]:
    """Expiring / expired vendor documents: notify the owner; suspend when a mandatory document has been
    expired
    for longer than ``suspension_grace_days`` (actor ``system``)."""
    from app.services import notifications_service as ns

    day = today or now().date()
    settings = await load_settings(db)
    mandatory = set(await mandatory_categories(db))
    grace = settings.suspension_grace_days
    vendors = (
        (await db.execute(select(Vendor).where(Vendor.status.in_(("onboarding", "active"))))).scalars().all()
    )
    suspended: list[str] = []
    alerts = 0
    for v in vendors:
        docs = await _live_docs(db, v.id)
        for d in docs:
            if d.expires_on is None:
                continue
            if d.expires_on in (day + timedelta(days=EXPIRING_DAYS), day - timedelta(days=1)) and v.owner_id:
                alerts += 1
                state = "expires on" if d.expires_on >= day else "expired on"
                await ns.deliver(
                    db,
                    "vendors.document_expiry",
                    {v.owner_id},
                    ns.Content(
                        title=f"{v.legal_name}: {d.category} document {state} {d.expires_on.isoformat()}"[
                            :300
                        ],
                        link_path=f"/vendors/{v.id}",
                    ),
                    event_id=f"vendor-doc:{d.id}:{d.expires_on.isoformat()}:{day.isoformat()}",
                )
        if v.status != "active":
            continue
        for cat in sorted(mandatory):
            cat_docs = [d for d in docs if d.category == cat]
            if not cat_docs:
                continue
            latest = max((d.expires_on for d in cat_docs if d.expires_on is not None), default=None)
            if any(d.expires_on is None for d in cat_docs) or latest is None:
                continue
            if (day - latest).days > grace:
                await transition_vendor(
                    ctx, db, v.id, "suspended", ActorRef("system", None), reason=f"document_expired:{cat}"
                )
                suspended.append(str(v.id))
                break
    return {"alerts": alerts, "suspended": suspended}


# ================================================================ files_service access checker ``vendor``
async def file_access(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID, write: bool) -> bool:
    from app.core.permissions import Principal

    principal = ctx.principal
    if principal is None:
        return True
    if not isinstance(principal, Principal):
        return False
    if principal.type == "vendor_user":
        if principal.org_id != vendor_id:
            return False
        return not write or "documents:manage" in principal.portal_permissions
    if principal.type != "user":
        return False
    exists = (
        await db.execute(select(func.count()).select_from(Vendor).where(Vendor.id == vendor_id))
    ).scalar_one()
    if not exists:
        return False
    return principal.scope_of("vendor:write" if write else "vendor:read") is not None


async def open_release_count(db: AsyncSession, vendor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(VendorRelease.vendor_id, func.count())
        .where(VendorRelease.vendor_id.in_(list(vendor_ids)), VendorRelease.status.in_(OPEN_RELEASE_STATUSES))
        .group_by(VendorRelease.vendor_id)
    )
    return {vid: int(n) for vid, n in rows.tuples()}


# ================================================================ settings
def settings_out(row: Any) -> VendorSettingsOut:
    from app.services.vendors.common import labels_of

    return VendorSettingsOut(
        ownership_window_days=row.ownership_window_days,
        duplicate_activity_months=row.duplicate_activity_months,
        suspension_grace_days=row.suspension_grace_days,
        mandatory_document_kinds=list(row.mandatory_document_kinds or []),
        scorecard_visible_to_vendor=row.scorecard_visible_to_vendor,
        vendor_labels=labels_of(row),
        notice_link_days=row.notice_link_days,
        require_bench_review_for_vendor_release=row.require_bench_review_for_vendor_release,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> VendorSettingsOut:  # ruff: ignore[unused-function-argument]
    return settings_out(await load_settings(db))


async def update_settings(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    data: VendorSettingsUpdate,
) -> VendorSettingsOut:
    row = await load_settings(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(row, key, list(value) if isinstance(value, list) else value)
    await db.flush()
    return settings_out(row)
