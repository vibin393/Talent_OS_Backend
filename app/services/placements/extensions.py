# ruff: noqa: E501
"""Extensions (FR-163, PRD §5.15, AD-4). ``status`` is written only through ``_transition`` against ``TRANSITIONS``.

* Propose: run the checks; an open offboarding case is 409 ``offboarding_in_progress``; a second open extension is
  409 ``extension_exists``. Rate / margin / PO fails become AD-24 approval steps (subject ``placement_extension``);
  without steps the extension goes straight to ``pending_client_confirmation``.
* Client confirmation: Client Portal (``client_user``), Secure Link (purpose ``extension_confirmation``) or
  recorded with evidence (``recorded_by_user``). ★ Confirm is human-only; ``ai`` / ``workflow`` / ``system`` -> 409.
* Confirm (one transaction): lock the placement, re-read the credential / PO gates and re-run the checks
  (``extension_blocked`` unless the exception approval -- subject ``extension_exception`` -- is approved;
  ``checks_changed`` on a new fail), move ``end_date`` (history row), append a rate segment, emit
  ``ExtensionConfirmed``, create the contract-renewal Task when ``contract_end`` warned.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.placements import ExtensionConfirmed, ExtensionStatusChanged
from app.models.placements import Placement
from app.models.placements_exit import EXTENSION_OPEN, Extension
from app.schemas.placements_exit import (
    CheckResult,
    ExtensionIn,
    ExtensionOut,
    ExtensionPreviewOut,
    LinkOut,
    RecordedResponseIn,
)
from app.services.placements import extension_checks
from app.services.placements.common import get_placement, local_today
from app.services.placements.exit_settings import set_end_date

SUBJECT = "placement_extension"
EXCEPTION_SUBJECT = "extension_exception"
LINK_PURPOSE = "extension_confirmation"
LINK_TTL = timedelta(days=14)
_CLIENT = frozenset({"user", "client_user", "secure_link", "recorded_by_user"})
_RESPONDERS = frozenset({"client_user", "secure_link", "recorded_by_user"})
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("proposed", "pending_approval"): frozenset({"system"}),
    ("proposed", "pending_client_confirmation"): frozenset({"system"}),
    ("pending_approval", "pending_client_confirmation"): frozenset({"system"}),
    ("pending_approval", "proposed"): frozenset({"system"}),
    ("pending_client_confirmation", "confirmed"): _CLIENT,
    ("pending_client_confirmation", "declined"): _RESPONDERS,
    ("proposed", "withdrawn"): frozenset({"user", "system"}),
    ("pending_approval", "withdrawn"): frozenset({"user", "system"}),
    ("pending_client_confirmation", "withdrawn"): frozenset({"user", "system"}),
}
HUMAN_ONLY = frozenset({("pending_client_confirmation", "confirmed")})
SYSTEM = ActorRef("system", None)


def _not_found() -> NotFound:
    return NotFound("Extension not found")


def not_pending() -> ConflictError:
    return ConflictError(
        "This extension is no longer awaiting a client response", code="extension_not_pending"
    )


def check_transition(from_status: str, to_status: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor_kind not in allowed:
        raise ConflictError(
            f"An extension cannot move from {from_status} to {to_status}",
            code="transition_not_allowed",
            details={"from": from_status, "to": to_status, "actor": actor_kind},
        )


async def _transition(db: AsyncSession, ext: Extension, to: str, actor: ActorRef) -> None:
    check_transition(ext.status, to, actor.kind)
    from_status = ext.status
    ext.status = to
    await db.flush()
    emit(
        db,
        ExtensionStatusChanged(
            tenant_id=ext.tenant_id,
            extension_id=ext.id,
            placement_id=ext.placement_id,
            from_status=from_status,
            to_status=to,
        ),
    )


def out(ext: Extension) -> ExtensionOut:
    return ExtensionOut.model_validate(ext)


async def get_row(db: AsyncSession, extension_id: uuid.UUID, *, lock: bool = False) -> Extension:
    stmt = select(Extension).where(Extension.id == extension_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def open_extension(db: AsyncSession, placement_id: uuid.UUID) -> Extension | None:
    return (
        await db.execute(
            select(Extension).where(
                Extension.placement_id == placement_id, Extension.status.in_(EXTENSION_OPEN)
            )
        )
    ).scalar_one_or_none()


def _proposal(placement: Placement, data: Any, current_end: Any = None) -> extension_checks.Proposal:
    return extension_checks.Proposal(
        current_end_date=current_end or placement.end_date or placement.planned_end_date,  # type: ignore[arg-type]
        new_end_date=data.new_end_date,
        segment_effective_from=data.segment_effective_from,
        rate_unit=data.rate_unit,
        pay_rate=data.pay_rate,
        bill_rate=data.bill_rate,
        vendor_rate=data.vendor_rate,
        burden_pct=data.burden_pct,
        po_id=data.po_id,
    )


def _validate(placement: Placement, data: ExtensionIn) -> None:
    if placement.status != "active":
        raise ConflictError("Only active placements can be extended", code="placement_not_active")
    current = placement.end_date or placement.planned_end_date
    if current is None:
        raise ConflictError("The placement has no end date to extend", code="no_end_date")
    if data.new_end_date <= current:
        raise ValidationFailed(
            "The new end date must be after the current end date", code="new_end_date_invalid"
        )
    if data.segment_effective_from is not None and (data.segment_effective_from > data.new_end_date):
        raise ValidationFailed("The new rate must start before the new end date", code="segment_date_invalid")


async def preview(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: ExtensionIn
) -> ExtensionPreviewOut:
    from app.services.placements.lists import visible

    placement = await visible(ctx, db, placement_id)
    _validate(placement, data)
    checks, amount, pct = await extension_checks.run(ctx, db, placement, _proposal(placement, data))
    blocking = any(c.status == "fail" and not c.overridable for c in checks)
    return ExtensionPreviewOut(checks=checks, margin_amount=amount, margin_pct=pct, blocking=blocking)


async def _steps(db: AsyncSession, fails: set[str]) -> list[Any] | None:
    """Explicit AD-24 steps for the failing checks; ``None`` defers to an active ``placement_extension`` chain."""
    from app.models.approvals import ApprovalChain
    from app.services.approvals import StepSpec

    chain = (
        await db.execute(
            select(ApprovalChain.id).where(
                ApprovalChain.subject_type == SUBJECT, ApprovalChain.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if chain is not None:
        return None
    admin = {"role_key": "tenant_admin"}
    steps = []
    if "rate_card" in fails:
        steps.append(StepSpec(name="Rate exception", approver_kind="role", approver_ref=admin))
    if "margin" in fails:
        steps.append(StepSpec(name="Margin exception", approver_kind="role", approver_ref=admin))
    if fails & {"po_validity", "po_balance"}:
        steps.append(StepSpec(name="po_exception", approver_kind="role", approver_ref=admin))
    return steps


async def propose(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: ExtensionIn
) -> ExtensionOut:
    from app.services import approvals
    from app.services.placements.lists import get_or_404

    actor = actor_from_ctx(ctx)
    placement = await get_or_404(ctx, db, placement_id)
    _validate(placement, data)
    if await extension_checks.open_case(db, placement.id) is not None:
        raise ConflictError("An offboarding case is open; cancel it first", code="offboarding_in_progress")
    if await open_extension(db, placement.id) is not None:
        raise ConflictError("This placement already has an open extension", code="extension_exists")
    prop = _proposal(placement, data)
    checks, _, _ = await extension_checks.run(ctx, db, placement, prop)
    has_rate = any(v is not None for v in (data.pay_rate, data.bill_rate, data.vendor_rate))
    ext = Extension(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        human_id=await next_human_id(db, "extension", "EXT"),
        placement_id=placement.id,
        client_id=placement.client_id,
        candidate_id=placement.candidate_id,
        current_end_date=prop.current_end_date,
        new_end_date=data.new_end_date,
        segment_effective_from=(data.segment_effective_from or prop.current_end_date + timedelta(days=1))
        if has_rate
        else None,
        rate_unit=data.rate_unit,
        pay_rate=data.pay_rate,
        bill_rate=data.bill_rate,
        vendor_rate=data.vendor_rate,
        burden_pct=data.burden_pct,
        currency=placement.currency,
        po_id=data.po_id or placement.po_id,
        status="proposed",
        checks=[c.model_dump(mode="json") for c in checks],
        note=data.note,
        proposed_by=actor.id if actor.kind == "user" else None,
    )
    db.add(ext)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This placement already has an open extension", code="extension_exists") from exc
    emit(
        db,
        ExtensionStatusChanged(
            tenant_id=ext.tenant_id,
            extension_id=ext.id,
            placement_id=placement.id,
            from_status="none",
            to_status="proposed",
        ),
    )
    fails = extension_checks.failing(checks) & extension_checks.APPROVAL_CHECKS
    if fails:
        steps = await _steps(db, fails)
        facts = {
            "placement_id": str(placement.id),
            "client_id": str(placement.client_id),
            "fails": sorted(fails),
        }
        await _transition(db, ext, "pending_approval", SYSTEM)
        result = await approvals.start(ctx, db, SUBJECT, ext.id, facts, steps=steps)
        ext.approval_request_id = result.request_id
        await db.flush()
        # an auto-approved request already moved the extension through on_extension_decided
    else:
        await _transition(db, ext, "pending_client_confirmation", SYSTEM)
    await db.refresh(ext)
    return out(ext)


async def on_extension_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # noqa: ARG001
    ext = (
        await db.execute(select(Extension).where(Extension.id == decision.subject_id).with_for_update())
    ).scalar_one_or_none()
    if ext is None or ext.status != "pending_approval":
        return
    to = "pending_client_confirmation" if decision.outcome in ("approved", "auto_approved") else "proposed"
    await _transition(db, ext, to, SYSTEM)


async def on_exception_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # noqa: ARG001
    ext = (
        await db.execute(select(Extension).where(Extension.id == decision.subject_id).with_for_update())
    ).scalar_one_or_none()
    if ext is None or ext.exception_status != "pending":
        return
    ext.exception_status = "approved" if decision.outcome in ("approved", "auto_approved") else "rejected"
    await db.flush()


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:  # noqa: ARG001
    from app.schemas.approvals import SubjectSummary

    rows = (
        await db.execute(
            select(Extension, Placement.human_id)
            .join(Placement, Placement.id == Extension.placement_id)
            .where(Extension.id.in_(ids))
        )
    ).all()
    return {
        e.id: SubjectSummary(
            label="Placement extension", title=f"{human}: extend to {e.new_end_date} ({e.human_id})"
        )
        for e, human in rows
    }


async def list_for_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> list[ExtensionOut]:
    from app.services.placements.lists import visible

    await visible(ctx, db, placement_id)
    rows = (
        await db.execute(
            select(Extension)
            .where(Extension.placement_id == placement_id)
            .order_by(Extension.created_at.desc())
        )
    ).scalars()
    return [out(r) for r in rows]


async def _visible_ext(
    ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID, *, lock: bool = False
) -> Extension:
    from app.services.placements.lists import visible

    ext = await get_row(db, extension_id, lock=lock)
    try:
        await visible(ctx, db, ext.placement_id)
    except NotFound as exc:
        raise _not_found() from exc
    return ext


async def get(ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID) -> ExtensionOut:
    return out(await _visible_ext(ctx, db, extension_id))


async def recheck(ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID) -> ExtensionOut:
    ext = await _visible_ext(ctx, db, extension_id, lock=True)
    if ext.status not in EXTENSION_OPEN:
        raise not_pending()
    placement = await get_placement(db, ext.placement_id)
    checks, _, _ = await extension_checks.run(
        ctx, db, placement, _proposal(placement, ext, ext.current_end_date)
    )
    ext.checks = [c.model_dump(mode="json") for c in checks]
    await db.flush()
    await db.refresh(ext)
    return out(ext)


async def request_exception(
    ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID, reason: str
) -> ExtensionOut:
    from app.services import approvals
    from app.services.approvals import StepSpec

    ext = await _visible_ext(ctx, db, extension_id, lock=True)
    if ext.status not in EXTENSION_OPEN:
        raise not_pending()
    if ext.exception_status in ("pending", "approved"):
        raise ConflictError("An exception was already requested", code="exception_exists")
    fails = extension_checks.failing(ext.checks) & extension_checks.EXCEPTION_CHECKS
    if not fails:
        raise ConflictError("No check needs an exception", code="no_exception_needed")
    ext.exception_status = "pending"
    ext.exception_reason = reason
    await db.flush()
    facts = {"placement_id": str(ext.placement_id), "fails": sorted(fails), "reason": reason}
    result = await approvals.start(
        ctx,
        db,
        EXCEPTION_SUBJECT,
        ext.id,
        facts,
        steps=[
            StepSpec(
                name="Extension exception", approver_kind="role", approver_ref={"role_key": "tenant_admin"}
            )
        ],
    )
    ext.exception_approval_request_id = result.request_id
    await db.flush()
    await db.refresh(ext)
    return out(ext)


async def client_link(ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID) -> LinkOut:
    ext = await _visible_ext(ctx, db, extension_id, lock=True)
    if ext.status != "pending_client_confirmation":
        raise not_pending()
    await secure_links.revoke_for_subject(db, "extension", ext.id, LINK_PURPOSE)
    actor = actor_from_ctx(ctx)
    link, token = await secure_links.issue(
        db,
        tenant_id=ext.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type="extension",
        subject_id=ext.id,
        ttl=LINK_TTL,
        metadata={"placement_id": str(ext.placement_id), "client_id": str(ext.client_id)},
        created_by=actor.id if actor.kind == "user" else None,
    )
    return LinkOut(url=secure_links.link_url(token), expires_at=link.expires_at)


# ---------------------------------------------------------------- confirm / decline / withdraw
def _blocked(fails: set[str], ext: Extension) -> set[str]:
    """Credential / PO fails not covered by an approval: PO fails are covered by the extension approval (its
    ``po_exception`` step), everything by an approved exception request."""
    if ext.exception_status == "approved":
        return set()
    out_ = fails & extension_checks.EXCEPTION_CHECKS
    if ext.approval_request_id is not None:
        previously = extension_checks.failing(ext.checks)
        out_ -= {f for f in ("po_validity", "po_balance") if f in previously}
    return out_


async def _gates_blocked(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[Any]:
    from app.services.placements import gates
    from app.services.placements.registry import start_gate_kinds

    kinds = [k for k in ("credential",) if k in start_gate_kinds()]
    if not kinds:
        return []
    try:
        async with db.begin_nested():
            await gates.assert_clear_for_update(ctx, db, placement_id, kinds)
    except ConflictError as exc:
        return list((exc.details or {}).get("gates") or [])
    return []


async def _renewal_task(
    ctx: RequestContext, db: AsyncSession, placement: Placement, check: dict[str, Any]
) -> None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    owner = placement.account_manager_user_id or placement.recruiter_user_id
    if owner is None:
        return
    try:
        async with db.begin_nested():
            await tasks_service.create_system_task(
                ctx,
                db,
                TaskCreate(
                    title=f"Renew contract: placement {placement.human_id} extended beyond {check['details'].get('contract_end')}"[
                        :200
                    ],
                    owner_id=owner,
                    entity_type="client",
                    entity_id=placement.client_id,
                    priority="high",
                ),
                source="offboarding",
            )
    except Exception:
        logger.exception("renewal task not created for placement {}", placement.id)


async def confirm(
    ctx: RequestContext,
    db: AsyncSession,
    extension_id: uuid.UUID,
    actor: ActorRef,
    *,
    send_extension_letter: bool = False,
    comment: str | None = None,
    evidence_file_id: uuid.UUID | None = None,
) -> Extension:
    from app.schemas.placements_api import RateSegmentIn
    from app.services.placements import rate_segments

    ext = await get_row(db, extension_id)
    placement = await get_placement(db, ext.placement_id, lock=True)
    ext = await get_row(db, extension_id, lock=True)
    if ext.status != "pending_client_confirmation":
        raise not_pending()
    check_transition(ext.status, "confirmed", actor.kind)
    gate_fails = await _gates_blocked(ctx, db, placement.id)
    checks, _, _ = await extension_checks.run(
        ctx, db, placement, _proposal(placement, ext, ext.current_end_date)
    )
    now_fails = extension_checks.failing(checks)
    if gate_fails:
        now_fails.add("credentials")
    blocked = _blocked(now_fails, ext)
    if blocked:
        raise ConflictError(
            "The extension is blocked by credential or PO checks",
            code="extension_blocked",
            details={"checks": sorted(blocked), "gates": gate_fails},
        )
    new = now_fails - extension_checks.failing(ext.checks) - extension_checks.EXCEPTION_CHECKS
    if new:
        ext.checks = [c.model_dump(mode="json") for c in checks]
        raise ConflictError(
            "The checks changed since the proposal", code="checks_changed", details={"checks": sorted(new)}
        )
    if placement.status != "active":
        raise ConflictError("Only active placements can be extended", code="placement_not_active")
    await set_end_date(db, placement, ext.new_end_date, "extension", ext.id, actor)
    if any(v is not None for v in (ext.pay_rate, ext.bill_rate, ext.vendor_rate)):
        seg = await rate_segments.add_segment(
            ctx,
            db,
            placement.id,
            RateSegmentIn(
                effective_from=ext.segment_effective_from or ext.current_end_date + timedelta(days=1),
                pay_rate=ext.pay_rate,
                bill_rate=ext.bill_rate,
                vendor_rate=ext.vendor_rate,
                burden_pct=ext.burden_pct,
                rate_unit=ext.rate_unit,  # type: ignore[arg-type]
                reason=f"Extension {ext.human_id}",
            ),
            actor=actor if actor.kind == "user" else SYSTEM,
        )
        ext.rate_segment_id = seg.id
    ext.checks = [c.model_dump(mode="json") for c in checks]
    ext.confirmed_at = datetime.now(UTC)
    ext.confirmed_by = actor.id if actor.kind == "user" else None
    ext.send_extension_letter = send_extension_letter
    if actor.kind in _RESPONDERS:
        ext.client_response_by_kind = actor.kind
        ext.client_response_by_id = actor.id
        ext.client_response_at = ext.confirmed_at
        ext.client_comment = comment
        ext.client_evidence_file_id = evidence_file_id
    await _transition(db, ext, "confirmed", actor)
    await secure_links.revoke_for_subject(db, "extension", ext.id, LINK_PURPOSE)
    emit(
        db,
        ExtensionConfirmed(
            tenant_id=ext.tenant_id,
            extension_id=ext.id,
            placement_id=placement.id,
            new_end_date=ext.new_end_date,
            rate_segment_id=ext.rate_segment_id,
        ),
    )
    warn = next((c for c in ext.checks if c["name"] == "contract_end" and c["status"] == "warn"), None)
    if warn is not None and warn["details"].get("contract_end"):
        await _renewal_task(ctx, db, placement, warn)
    await db.flush()
    return ext


async def decline(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    extension_id: uuid.UUID,
    actor: ActorRef,
    *,
    comment: str | None = None,
    evidence_file_id: uuid.UUID | None = None,
) -> Extension:
    ext = await get_row(db, extension_id, lock=True)
    if ext.status != "pending_client_confirmation":
        raise not_pending()
    ext.client_response_by_kind = actor.kind
    ext.client_response_by_id = actor.id
    ext.client_response_at = datetime.now(UTC)
    ext.client_comment = comment
    ext.client_evidence_file_id = evidence_file_id
    await _transition(db, ext, "declined", actor)
    await secure_links.revoke_for_subject(db, "extension", ext.id, LINK_PURPOSE)
    return ext


async def confirm_internal(
    ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID, send_letter: bool
) -> ExtensionOut:
    await _visible_ext(ctx, db, extension_id)
    ext = await confirm(ctx, db, extension_id, actor_from_ctx(ctx), send_extension_letter=send_letter)
    await db.refresh(ext)
    return out(ext)


async def record_response(
    ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID, data: RecordedResponseIn
) -> ExtensionOut:
    await _visible_ext(ctx, db, extension_id)
    user = actor_from_ctx(ctx)
    actor = ActorRef("recorded_by_user", user.id, evidence_file_id=data.evidence_file_id)
    if data.decision == "confirm":
        ext = await confirm(
            ctx,
            db,
            extension_id,
            actor,
            send_extension_letter=data.send_extension_letter,
            comment=data.comment,
            evidence_file_id=data.evidence_file_id,
        )
    else:
        ext = await decline(
            ctx, db, extension_id, actor, comment=data.comment, evidence_file_id=data.evidence_file_id
        )
    await db.refresh(ext)
    return out(ext)


async def withdraw(
    ctx: RequestContext,
    db: AsyncSession,
    extension_id: uuid.UUID,
    reason: str,
    *,
    actor: ActorRef | None = None,
) -> Extension:
    from app.services import approvals

    if actor is None:
        await _visible_ext(ctx, db, extension_id)
    actor = actor or actor_from_ctx(ctx)
    ext = await get_row(db, extension_id, lock=True)
    if ext.status not in EXTENSION_OPEN:
        raise not_pending()
    ext.withdraw_reason = reason
    await _transition(db, ext, "withdrawn", actor)
    await secure_links.revoke_for_subject(db, "extension", ext.id)
    for subject in (SUBJECT, EXCEPTION_SUBJECT):
        try:
            await approvals.cancel_open(ctx, db, subject, ext.id)
        except Exception:
            logger.exception("approval cancel failed for extension {}", ext.id)
    return ext


async def withdraw_out(
    ctx: RequestContext, db: AsyncSession, extension_id: uuid.UUID, reason: str
) -> ExtensionOut:
    ext = await withdraw(ctx, db, extension_id, reason)
    await db.refresh(ext)
    return out(ext)


async def end_date_series_blocked(db: AsyncSession, placement_id: uuid.UUID, end_date: Any) -> bool:
    """Tell whether an open or confirmed extension exists for this end date (alerts are suppressed)."""
    row = (
        await db.execute(
            select(Extension.id)
            .where(
                Extension.placement_id == placement_id,
                Extension.current_end_date == end_date,
                Extension.status.in_((*EXTENSION_OPEN, "confirmed")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


def check_list(raw: list[dict[str, Any]]) -> list[CheckResult]:
    return [CheckResult.model_validate(c) for c in raw]


async def today_for(db: AsyncSession, placement: Placement) -> Any:
    return await local_today(db, placement)
