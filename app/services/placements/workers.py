"""Worker profile, PAN / bank details and Worker 360 (Epic 19 A8, P2-6.5, FR-108). AD-27: a Worker is a Candidate
with a profile -- no worker principal.

* PAN / bank numbers are ``EncryptedStr`` (AD-35); only ``last4`` leaves the service unless ``unmask`` (requires
  ``identity:view``, audited ``identity_unmasked``). Values never reach logs, events or notifications.
* ``request_bank_change`` records a ``pending_approval`` row and starts approval ``bank_detail_change`` (explicit
  step: role ``hr_onboarding``); the final-approved callback activates it (superseding the old active row). The
  Worker is notified on request and on decision (fraud control).
"""

import re
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.encrypted import IDENTITY_VIEW, set_identity_field, unmask
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.placements import BankDetailsChangeDecided
from app.models.placements import Placement, WorkerBankAccount, WorkerProfile
from app.schemas.placements import WorkerPaymentIdentity
from app.schemas.placements_api import (
    BankAccountIn,
    BankAccountOut,
    PlacementListItem,
    UnmaskOut,
    WorkerOut,
    WorkerProfileOut,
    WorkerProfileUpdate,
)
from app.services.placements.common import names, now
from app.services.placements.events import BankDetailsChangeRequested
from app.services.placements.registry import worker_360_sections

BANK_SUBJECT = "bank_detail_change"
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
ACCOUNT_RE = re.compile(r"^[0-9]{6,20}$")


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def ensure_worker_profile(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    default_worker_type: str | None = None,
) -> WorkerProfile:
    row = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = WorkerProfile(
        tenant_id=_tenant(db),
        candidate_id=candidate_id,
        worker_human_id=await next_human_id(db, "worker", "WRK", width=6),
        default_worker_type=default_worker_type,
        pan_status="unverified",
    )
    async with db.begin_nested():
        db.add(row)
        await db.flush()
    return row


async def _profile(db: AsyncSession, candidate_id: uuid.UUID) -> WorkerProfile:
    row = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Worker not found")
    return row


async def update_profile(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: WorkerProfileUpdate | dict[str, Any]
) -> WorkerProfileOut:
    profile = await ensure_worker_profile(ctx, db, candidate_id)
    changes = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    for key in (
        "emergency_name",
        "emergency_relation",
        "emergency_phone",
        "manager_user_id",
        "default_worker_type",
    ):
        if key in changes:
            setattr(profile, key, changes[key] or None)
    await db.flush()
    return WorkerProfileOut.model_validate(profile)


def validate_ifsc(ifsc: str) -> str:
    value = (ifsc or "").strip().upper()
    if not IFSC_RE.match(value):
        raise ValidationFailed(
            "Enter a valid IFSC (e.g. HDFC0001234)", code="invalid_ifsc", details={"field": "ifsc"}
        )
    return value


def validate_pan(pan: str) -> str:
    value = (pan or "").strip().upper()
    if not PAN_RE.match(value):
        raise ValidationFailed(
            "Enter a valid PAN (e.g. ABCDE1234F)", code="invalid_pan", details={"field": "pan"}
        )
    return value


async def record_bank_details(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: BankAccountIn, source: str
) -> WorkerBankAccount:
    """Record a ``pending_approval`` row (the account number is entered twice and must match)."""
    if data.account_number.replace(" ", "") != data.account_number_confirm.replace(" ", ""):
        raise ValidationFailed(
            "The account numbers do not match",
            code="account_mismatch",
            details={"field": "account_number_confirm"},
        )
    number = data.account_number.replace(" ", "")
    if not ACCOUNT_RE.match(number):
        raise ValidationFailed(
            "Enter a valid account number", code="invalid_account_number", details={"field": "account_number"}
        )
    ifsc = validate_ifsc(data.ifsc)
    await ensure_worker_profile(ctx, db, candidate_id)
    row = WorkerBankAccount(
        tenant_id=_tenant(db),
        candidate_id=candidate_id,
        holder_name=data.holder_name,
        ifsc=ifsc,
        bank_name=data.bank_name,
        status="pending_approval",
        source=source,
    )
    set_identity_field(row, "account_number", number, row.tenant_id)
    db.add(row)
    await db.flush()
    return row


async def request_bank_change(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: BankAccountIn, source: str
) -> WorkerBankAccount:
    from app.services import approvals
    from app.services.approvals import StepSpec

    row = await record_bank_details(ctx, db, candidate_id, data, source)
    steps = [StepSpec(name="hr", approver_kind="role", approver_ref={"role_key": "hr_onboarding"})]
    result = await approvals.start(
        ctx, db, BANK_SUBJECT, row.id, {"candidate_id": str(candidate_id)}, steps=steps
    )
    if result.request_id is not None:
        row.approval_request_id = result.request_id
    await db.flush()
    emit(
        db,
        BankDetailsChangeRequested(
            tenant_id=row.tenant_id,
            candidate_id=candidate_id,
            bank_account_id=row.id,
            last4=row.account_number_last4,
        ),
    )
    return row


async def activate_bank_details(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    bank_account_id: uuid.UUID,
    actor: Any = None,  # noqa: ARG001
) -> WorkerBankAccount:
    row = (
        await db.execute(
            select(WorkerBankAccount).where(WorkerBankAccount.id == bank_account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Bank account not found")
    if row.status == "active":
        return row
    current = (
        await db.execute(
            select(WorkerBankAccount)
            .where(WorkerBankAccount.candidate_id == row.candidate_id, WorkerBankAccount.status == "active")
            .with_for_update()
        )
    ).scalars()
    for old in current:
        old.status = "superseded"
    await db.flush()
    row.status = "active"
    row.activated_at = now()
    row.decided_at = now()
    await db.flush()
    return row


async def on_bank_change_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    """Apply the final decision of ``bank_detail_change`` (decision transaction)."""
    row = (
        await db.execute(
            select(WorkerBankAccount).where(WorkerBankAccount.id == decision.subject_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != "pending_approval":
        return
    approved = decision.outcome in ("approved", "auto_approved")
    if approved:
        await activate_bank_details(ctx, db, row.id, decision.actor)
    else:
        row.status = "rejected"
        row.decided_at = now()
        await db.flush()
    emit(
        db,
        BankDetailsChangeDecided(
            tenant_id=row.tenant_id,
            candidate_id=row.candidate_id,
            bank_account_id=row.id,
            outcome="approved" if approved else "rejected",
        ),
    )


async def describe_bank_changes(
    ctx: RequestContext,
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, Any]:
    from app.schemas.approvals import SubjectSummary

    rows = (await db.execute(select(WorkerBankAccount).where(WorkerBankAccount.id.in_(ids)))).scalars()
    return {
        r.id: SubjectSummary(
            label="Bank details change", title=f"Account ending {r.account_number_last4} ({r.ifsc})"
        )
        for r in rows
    }


async def record_pan(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, pan: str, pan_name: str | None = None
) -> WorkerProfileOut:
    value = validate_pan(pan)
    profile = await ensure_worker_profile(ctx, db, candidate_id)
    set_identity_field(profile, "pan", value, profile.tenant_id)
    profile.pan_name = pan_name
    profile.pan_status = "unverified"
    await db.flush()
    return WorkerProfileOut.model_validate(profile)


async def confirm_pan(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    actor: Any = None,  # noqa: ARG001
) -> WorkerProfileOut:
    """HR records the name-match check (``pan_status = hr_checked``)."""
    profile = await _profile(db, candidate_id)
    if profile.pan is None:
        raise ValidationFailed("No PAN recorded", code="pan_missing")
    profile.pan_status = "hr_checked"
    await db.flush()
    return WorkerProfileOut.model_validate(profile)


async def _active_bank(db: AsyncSession, candidate_id: uuid.UUID) -> WorkerBankAccount | None:
    return (
        await db.execute(
            select(WorkerBankAccount).where(
                WorkerBankAccount.candidate_id == candidate_id, WorkerBankAccount.status == "active"
            )
        )
    ).scalar_one_or_none()


async def worker_payment_identity(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, unmask: bool = False
) -> WorkerPaymentIdentity:
    profile = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    bank = await _active_bank(db, candidate_id)
    pan = number = None
    if unmask:
        if profile is not None and profile.pan is not None:
            pan = await _unmask(ctx, db, profile, "pan")
        if bank is not None and bank.account_number is not None:
            number = await _unmask(ctx, db, bank, "account_number")
    return WorkerPaymentIdentity(
        candidate_id=candidate_id,
        pan_last4=profile.pan_last4 if profile else None,
        pan_status=profile.pan_status if profile else None,
        bank_account_id=bank.id if bank else None,
        bank_account_last4=bank.account_number_last4 if bank else None,
        ifsc=bank.ifsc if bank else None,
        bank_active=bank is not None,
        pan=pan,
        account_number=number,
    )


async def _unmask(ctx: RequestContext, db: AsyncSession, obj: Any, field: str) -> str:
    principal = ctx.principal
    has = getattr(principal, "has", None)
    if not callable(has) or not has(IDENTITY_VIEW):
        raise Forbidden("You do not have permission to view this value", required_permission=IDENTITY_VIEW)
    return await unmask(ctx, db, obj, field)


async def unmask_field(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    field: str,
    bank_account_id: uuid.UUID | None,
) -> UnmaskOut:
    if field == "pan":
        profile = await _profile(db, candidate_id)
        if profile.pan is None:
            raise NotFound("No PAN recorded")
        return UnmaskOut(field="pan", value=await _unmask(ctx, db, profile, "pan"))
    stmt = select(WorkerBankAccount).where(WorkerBankAccount.candidate_id == candidate_id)
    stmt = (
        stmt.where(WorkerBankAccount.id == bank_account_id)
        if bank_account_id
        else stmt.where(WorkerBankAccount.status == "active")
    )
    bank = (await db.execute(stmt)).scalar_one_or_none()
    if bank is None or bank.account_number is None:
        raise NotFound("Bank account not found")
    return UnmaskOut(field="bank_account", value=await _unmask(ctx, db, bank, "account_number"))


async def bank_accounts(db: AsyncSession, candidate_id: uuid.UUID) -> list[BankAccountOut]:
    rows = (
        await db.execute(
            select(WorkerBankAccount)
            .where(WorkerBankAccount.candidate_id == candidate_id)
            .order_by(WorkerBankAccount.created_at.desc())
        )
    ).scalars()
    return [BankAccountOut.model_validate(r) for r in rows]


async def get_worker(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> WorkerOut:
    from app.services import entity_refs
    from app.services.placements.lists import list_item

    who = await entity_refs.addressee(db, "candidate", candidate_id)
    profile = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    placements = list(
        (
            await db.execute(
                select(Placement)
                .where(Placement.candidate_id == candidate_id)
                .order_by(Placement.planned_start_date.desc())
            )
        ).scalars()
    )
    if who is None or (profile is None and not placements):
        raise NotFound("Worker not found")
    _cands, clients = await names(db, client_ids={p.client_id for p in placements})
    items: list[PlacementListItem] = [
        list_item(p, who.full_name, clients.get(p.client_id)) for p in placements
    ]
    current = next((p.id for p in placements if p.status in ("active", "offboarding")), None) or next(
        (p.id for p in placements if p.status in ("pre_joining", "joining_confirmed")), None
    )
    sections: dict[str, Any] = {}
    for key, loader in worker_360_sections().items():
        try:
            async with db.begin_nested():
                sections[key] = await loader(ctx, db, candidate_id)
        except Exception:
            logger.exception("worker 360 section {} failed", key)
    return WorkerOut(
        candidate_id=candidate_id,
        full_name=who.full_name,
        email=who.address,
        profile=WorkerProfileOut.model_validate(profile) if profile else None,
        placements=items,
        current_placement_id=current,
        bank_accounts=await bank_accounts(db, candidate_id),
        sections=sections,
    )
