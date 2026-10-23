# ruff: file-ignore[line-too-long]
"""Offboarding Cases (FR-165, PRD §5.16, AD-4). ``status`` is written only through ``_transition``.

* ``initiate`` (one transaction): lock the placement, refuse a second open case, snapshot ``previous_end_date``
  and set ``end_date = last_working_day`` (history row; time refuses later entries), Active -> Offboarding
  (system), materialise the template items with owners / due dates / P1 Tasks, withdraw any open extension and
  revoke its links, emit ``OffboardingCaseStatusChanged``; then run the verifiers once.
* ``refresh``: re-runs the **registered** verifiers (``placements.registry.offboarding_verifiers``, R18) and
  toggles system items done <-> pending; placements never imports time / payroll / billing / expenses / documents.
* ★ ``close`` is human-only (``user`` with ``offboarding:close``): re-runs the verifiers in the transaction
  (``items_incomplete`` on a regression), Offboarding -> Ended, ``bench_eligible``, revokes links, emits
  ``PlacementEnded``.
* ``cancel``: only while ``today < last_working_day`` (else ``past_last_working_day``); restores the end date and
  Active.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.placements import OffboardingCaseStatusChanged, PlacementEnded
from app.models.placements import Placement
from app.models.placements_exit import (
    CASE_OPEN,
    Conversion,
    OffboardingCase,
    OffboardingFeedback,
    OffboardingItem,
)
from app.schemas.placements_exit import (
    ConversionOut,
    FeedbackOut,
    LinkOut,
    OffboardingCaseDetail,
    OffboardingCaseOut,
    OffboardingItemOut,
    SettlementLine,
)
from app.services.placements import exit_settings
from app.services.placements.common import get_placement, local_today, names
from app.services.placements.exit_events import OffboardingItemCompleted
from app.services.placements.status import transition_placement

SYSTEM = ActorRef("system", None)
LINK_PURPOSE = "offboarding_confirmation"
LINK_TTL = timedelta(days=14)
CLEARED = ("done", "waived", "not_applicable")
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("initiated", "in_progress"): frozenset({"system"}),
    ("initiated", "ready_to_close"): frozenset({"system"}),
    ("in_progress", "ready_to_close"): frozenset({"system"}),
    ("ready_to_close", "in_progress"): frozenset({"system"}),
    ("ready_to_close", "closed"): frozenset({"user"}),
    ("initiated", "cancelled"): frozenset({"user"}),
    ("in_progress", "cancelled"): frozenset({"user"}),
}
HUMAN_ONLY = frozenset({("ready_to_close", "closed")})
SETTLEMENT_KINDS = {
    "final_timesheet_approved": "/timesheets?placement_id={pid}",
    "final_expense_settlement": "/expenses?placement_id={pid}",
    "final_payroll_inclusion": "/payroll?placement_id={pid}",
    "final_invoice": "/billing/runs?placement_id={pid}",
}


def _not_found() -> NotFound:
    return NotFound("Offboarding case not found")


def check_transition(from_status: str, to_status: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor_kind not in allowed:
        raise ConflictError(
            f"An offboarding case cannot move from {from_status} to {to_status}",
            code="transition_not_allowed",
            details={"from": from_status, "to": to_status, "actor": actor_kind},
        )


async def _transition(db: AsyncSession, case: OffboardingCase, to: str, actor: ActorRef) -> None:
    check_transition(case.status, to, actor.kind)
    from_status = case.status
    case.status = to
    await db.flush()
    emit(
        db,
        OffboardingCaseStatusChanged(
            tenant_id=case.tenant_id,
            case_id=case.id,
            placement_id=case.placement_id,
            from_status=from_status,
            to_status=to,
        ),
    )


def bench_eligible(worker_type: str | None, reason: str, excluded: list[str]) -> bool:
    return worker_type == "employee" and reason not in set(excluded)


def notice_shortfall(
    notice_period_days: int | None, notice_date: date | None, requested_lwd: date | None
) -> int | None:
    if notice_period_days is None or notice_date is None or requested_lwd is None:
        return None
    return max(0, notice_period_days - (requested_lwd - notice_date).days)


def is_early_attrition(actual_start: date | None, last_working_day: date, threshold_days: int) -> bool:
    return actual_start is not None and (last_working_day - actual_start).days < threshold_days


async def get_case(db: AsyncSession, case_id: uuid.UUID, *, lock: bool = False) -> OffboardingCase:
    stmt = select(OffboardingCase).where(OffboardingCase.id == case_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def _visible_case(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, *, lock: bool = False
) -> OffboardingCase:
    from app.services.placements.lists import visible

    case = await get_case(db, case_id, lock=lock)
    try:
        await visible(ctx, db, case.placement_id)
    except NotFound as exc:
        raise _not_found() from exc
    return case


async def _items(db: AsyncSession, case_id: uuid.UUID) -> list[OffboardingItem]:
    return list(
        (
            await db.execute(
                select(OffboardingItem)
                .where(OffboardingItem.case_id == case_id)
                .order_by(OffboardingItem.position)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _create_task(
    ctx: RequestContext, db: AsyncSession, placement: Placement, item: OffboardingItem
) -> uuid.UUID | None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    if item.owner_id is None:
        return None
    due = datetime.combine(item.due_date, datetime.min.time(), tzinfo=UTC) if item.due_date else None
    try:
        async with db.begin_nested():
            task = await tasks_service.create_system_task(
                ctx,
                db,
                TaskCreate(
                    title=f"Offboarding {placement.human_id}: {item.label}"[:200],
                    owner_id=item.owner_id,
                    entity_type="candidate",
                    entity_id=placement.candidate_id,
                    due_at=due,
                    due_all_day=due is not None,
                    priority="high" if item.mandatory else "normal",
                ),
                source="offboarding",
            )
        return task.id
    except Exception:
        logger.exception("offboarding task not created for item {}", item.kind)
        return None


async def _close_tasks(db: AsyncSession, task_ids: list[uuid.UUID], status: str = "cancelled") -> None:
    if not task_ids:
        return
    from app.models.tasks import Task

    await db.execute(update(Task).where(Task.id.in_(task_ids), Task.status == "open").values(status=status))


async def initiate(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    reason: str,
    last_working_day: date,
    *,
    actor: ActorRef | None = None,
    notice_date: date | None = None,
    requested_lwd: date | None = None,
    reason_note: str | None = None,
    template_id: uuid.UUID | None = None,
    early_termination_by: str | None = None,
    notice_period_days: int | None = None,
    bench_eligible_override: bool | None = None,
) -> OffboardingCase:
    from app.services.placements import extension_checks, extensions

    actor = actor or actor_from_ctx(ctx)
    placement = await get_placement(db, placement_id, lock=True)
    if await extension_checks.open_case(db, placement.id) is not None:
        raise ConflictError(
            "This placement already has an open offboarding case", code="offboarding_in_progress"
        )
    if placement.status != "active":
        raise ConflictError("Only active placements can be offboarded", code="placement_not_active")
    if placement.actual_start_date is not None and last_working_day < placement.actual_start_date:
        raise ValidationFailed(
            "The last working day is before the start date", code="last_working_day_invalid"
        )
    settings = await exit_settings.settings_row(db, placement.tenant_id)
    template = (
        await exit_settings.get_template(db, template_id)
        if template_id
        else await exit_settings.default_template(db, placement.tenant_id)
    )
    case = OffboardingCase(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        human_id=await next_human_id(db, "offboarding_case", "OFF"),
        placement_id=placement.id,
        client_id=placement.client_id,
        candidate_id=placement.candidate_id,
        template_id=template.id,
        reason=reason,
        reason_note=reason_note,
        notice_date=notice_date,
        requested_lwd=requested_lwd,
        last_working_day=last_working_day,
        previous_end_date=placement.end_date,
        notice_period_days_snapshot=notice_period_days,
        notice_shortfall_days=notice_shortfall(notice_period_days, notice_date, requested_lwd),
        early_termination_by=early_termination_by,
        early_attrition=is_early_attrition(
            placement.actual_start_date, last_working_day, settings.early_attrition_days
        ),
        bench_eligible=bench_eligible_override,
        status="initiated",
        initiated_by_kind=actor.kind,
        initiated_by=actor.id if actor.kind == "user" else None,
    )
    db.add(case)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "This placement already has an open offboarding case", code="offboarding_in_progress"
        ) from exc
    await exit_settings.set_end_date(db, placement, last_working_day, "offboarding", case.id, actor)
    await transition_placement(ctx, db, placement, "offboarding", SYSTEM, reason)
    raw_items = list(template.items or [])
    owners = await exit_settings.resolve_owners(
        db, placement, {i.get("owner_role") or "account_manager" for i in raw_items}
    )
    for pos, raw in enumerate(raw_items):
        item = OffboardingItem(
            id=uuid.uuid4(),
            tenant_id=placement.tenant_id,
            case_id=case.id,
            client_id=placement.client_id,
            candidate_id=placement.candidate_id,
            position=pos,
            kind=raw["kind"],
            label=raw["label"],
            mandatory=bool(raw.get("mandatory", True)),
            owner_role=raw.get("owner_role") or "account_manager",
            owner_id=owners.get(raw.get("owner_role") or "account_manager"),
            due_date=last_working_day + timedelta(days=int(raw.get("due_offset_days") or 0)),
            verifier=raw.get("verifier"),
            status="pending",
        )
        db.add(item)
        await db.flush()
        item.task_id = await _create_task(ctx, db, placement, item)
    await db.flush()
    open_ext = await extensions.open_extension(db, placement.id)
    if open_ext is not None:
        await extensions.withdraw(ctx, db, open_ext.id, f"Offboarding {case.human_id} started", actor=SYSTEM)
    emit(
        db,
        OffboardingCaseStatusChanged(
            tenant_id=case.tenant_id,
            case_id=case.id,
            placement_id=placement.id,
            from_status="none",
            to_status="initiated",
        ),
    )
    await refresh_case(ctx, db, case)
    return case


# ---------------------------------------------------------------- verifiers
async def _run_verifiers(db: AsyncSession, case: OffboardingCase, items: list[OffboardingItem]) -> bool:
    """Toggle system items from the registered verifiers. Returns True when any item regressed."""
    from app.services.placements.registry import offboarding_verifiers

    registry = offboarding_verifiers()
    regressed = False
    for item in items:
        fn = registry.get(item.verifier or "") if item.verifier else None
        if fn is None or item.status in ("waived", "not_applicable"):
            continue
        if item.status == "done" and not item.auto_verified:
            continue
        try:
            async with db.begin_nested():
                result = await fn(db, case.placement_id, case.last_working_day)
        except Exception as exc:
            logger.exception("offboarding verifier {} failed", item.verifier)
            item.verifier_details = {"error": type(exc).__name__}
            continue
        item.verifier_details = dict(result.details or {})
        if result.done and item.status == "pending":
            item.status = "done"
            item.auto_verified = True
            item.completed_by_kind = "system"
            item.completed_at = datetime.now(UTC)
            emit(
                db,
                OffboardingItemCompleted(
                    tenant_id=case.tenant_id,
                    case_id=case.id,
                    placement_id=case.placement_id,
                    item_id=item.id,
                    kind=item.kind,
                    status="done",
                    completed_by_kind="system",
                ),
            )
            await _close_tasks(db, [item.task_id] if item.task_id else [], "done")
        elif not result.done and item.status == "done":
            item.status = "pending"
            item.auto_verified = False
            item.completed_by_kind = None
            item.completed_at = None
            regressed = True
    await db.flush()
    return regressed


async def _settle_status(db: AsyncSession, case: OffboardingCase, items: list[OffboardingItem]) -> None:
    if case.status not in CASE_OPEN:
        return
    ready = all(i.status in CLEARED for i in items if i.mandatory)
    any_done = any(i.status in CLEARED for i in items)
    if ready and case.status != "ready_to_close":
        await _transition(db, case, "ready_to_close", SYSTEM)
    elif (not ready and case.status == "ready_to_close") or (
        not ready and any_done and case.status == "initiated"
    ):
        await _transition(db, case, "in_progress", SYSTEM)


async def refresh_case(ctx: RequestContext, db: AsyncSession, case: OffboardingCase) -> bool:  # ruff: ignore[unused-function-argument]
    if case.status not in CASE_OPEN:
        return False
    items = await _items(db, case.id)
    regressed = await _run_verifiers(db, case, items)
    await _settle_status(db, case, items)
    return regressed


async def refresh(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> None:
    """Re-run the verifiers of the placement's open case (event handlers, nightly beat)."""
    from app.services.placements.extension_checks import open_case

    case = await open_case(db, placement_id)
    if case is not None:
        case = await get_case(db, case.id, lock=True)
        await refresh_case(ctx, db, case)


async def refresh_all(ctx: RequestContext, db: AsyncSession, *, client_id: uuid.UUID | None = None) -> int:
    stmt = select(OffboardingCase.placement_id).where(OffboardingCase.status.in_(CASE_OPEN))
    if client_id is not None:
        stmt = stmt.where(OffboardingCase.client_id == client_id)
    ids = list((await db.execute(stmt)).scalars())
    for pid in ids:
        await refresh(ctx, db, pid)
    return len(ids)


# ---------------------------------------------------------------- items
async def _item(
    ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID
) -> tuple[OffboardingItem, OffboardingCase]:
    item = (
        await db.execute(select(OffboardingItem).where(OffboardingItem.id == item_id).with_for_update())
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("Offboarding item not found")
    try:
        case = await _visible_case(ctx, db, item.case_id, lock=True)
    except NotFound as exc:
        raise NotFound("Offboarding item not found") from exc
    if case.status not in CASE_OPEN:
        raise ConflictError("The offboarding case is not open", code="case_not_open")
    return item, case


async def complete_item(
    ctx: RequestContext,
    db: AsyncSession,
    item_id: uuid.UUID,
    *,
    evidence_file_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    note: str | None = None,
    actor: ActorRef | None = None,
) -> OffboardingCase:
    from app.services.placements.registry import offboarding_verifiers

    actor = actor or actor_from_ctx(ctx)
    item, case = await _item(ctx, db, item_id)
    if item.verifier and item.verifier in offboarding_verifiers():
        raise ConflictError("This item is verified automatically", code="item_auto_verified")
    if item.status != "pending":
        raise ConflictError("This item is already completed", code="item_not_pending")
    if (
        item.kind in ("relieving_letter", "experience_letter")
        and document_id is None
        and evidence_file_id is None
    ):
        raise ValidationFailed("A generated document is required", code="document_required")
    await _mark_done(
        db,
        case,
        item,
        actor.kind if actor.kind in ("user", "client_user", "secure_link") else "user",
        actor.id,
        evidence_file_id=evidence_file_id,
        document_id=document_id,
        note=note,
    )
    if item.kind == "access_removal":
        await secure_links.revoke_for_subject(db, "placement", case.placement_id)
    if item.kind == "client_confirmation":
        case.client_confirmed_at = item.completed_at
    await _settle_status(db, case, await _items(db, case.id))
    return case


async def _mark_done(
    db: AsyncSession,
    case: OffboardingCase,
    item: OffboardingItem,
    kind: str,
    by: uuid.UUID | None,
    *,
    evidence_file_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    item.status = "done"
    item.completed_by_kind = kind
    item.completed_by_id = by
    item.completed_at = datetime.now(UTC)
    item.evidence_file_id = evidence_file_id
    item.document_id = document_id
    item.note = note
    await db.flush()
    await _close_tasks(db, [item.task_id] if item.task_id else [], "done")
    emit(
        db,
        OffboardingItemCompleted(
            tenant_id=case.tenant_id,
            case_id=case.id,
            placement_id=case.placement_id,
            item_id=item.id,
            kind=item.kind,
            status="done",
            completed_by_kind=kind,
        ),
    )


async def waive_item(
    ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, reason: str
) -> OffboardingCase:
    item, case = await _item(ctx, db, item_id)
    if item.status != "pending":
        raise ConflictError("This item is already completed", code="item_not_pending")
    if not reason.strip():
        raise ValidationFailed("A reason is required", code="reason_required")
    principal = ctx.principal
    if item.mandatory and not (principal is not None and principal.has("offboarding:close")):
        from app.core.permissions import forbidden

        raise forbidden("offboarding:close")
    actor = actor_from_ctx(ctx)
    item.status = "waived"
    item.waived_reason = reason
    item.completed_by_kind = "user"
    item.completed_by_id = actor.id
    item.completed_at = datetime.now(UTC)
    await db.flush()
    await _close_tasks(db, [item.task_id] if item.task_id else [])
    await _settle_status(db, case, await _items(db, case.id))
    return case


async def on_signature_completed(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    item_id: uuid.UUID,
    document_id: uuid.UUID,
) -> None:
    """``SignatureRequestCompleted(subject_kind='offboarding_item')``: the letter item is done."""
    item = (
        await db.execute(select(OffboardingItem).where(OffboardingItem.id == item_id).with_for_update())
    ).scalar_one_or_none()
    if item is None or item.status != "pending":
        return
    case = await get_case(db, item.case_id, lock=True)
    if case.status not in CASE_OPEN:
        return
    await _mark_done(db, case, item, "system", None, document_id=document_id)
    await _settle_status(db, case, await _items(db, case.id))


async def client_confirm(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    case_id: uuid.UUID,
    actor: ActorRef,
    comment: str | None,
) -> OffboardingCase:
    case = await get_case(db, case_id, lock=True)
    if case.status not in CASE_OPEN:
        raise ConflictError("The offboarding case is not open", code="case_not_open")
    item = next((i for i in await _items(db, case.id) if i.kind == "client_confirmation"), None)
    case.client_confirmed_at = datetime.now(UTC)
    if item is not None and item.status == "pending":
        await _mark_done(db, case, item, actor.kind, actor.id, note=comment)
    await secure_links.revoke_for_subject(db, "offboarding_case", case.id, LINK_PURPOSE)
    await _settle_status(db, case, await _items(db, case.id))
    return case


# ---------------------------------------------------------------- close / cancel / feedback / link
async def close(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, actor: ActorRef | None = None
) -> OffboardingCase:
    actor = actor or actor_from_ctx(ctx)
    case = await get_case(db, case_id)
    placement = await get_placement(db, case.placement_id, lock=True)
    case = await get_case(db, case_id, lock=True)
    check_transition(
        case.status if case.status == "ready_to_close" else "ready_to_close", "closed", actor.kind
    )
    if case.status != "ready_to_close":
        raise ConflictError("Mandatory items are still open", code="items_incomplete")
    items = await _items(db, case.id)
    await _run_verifiers(db, case, items)
    pending = [i.kind for i in items if i.mandatory and i.status not in CLEARED]
    if pending:
        raise ConflictError(
            "Mandatory items are still open", code="items_incomplete", details={"items": pending}
        )
    settings = await exit_settings.settings_row(db, case.tenant_id)
    eligible = (
        case.bench_eligible
        if case.bench_eligible is False
        else bench_eligible(placement.worker_type, case.reason, list(settings.bench_excluded_reasons or []))
    )
    case.bench_eligible = eligible
    case.closed_by = actor.id
    case.closed_at = datetime.now(UTC)
    await _transition(db, case, "closed", actor)
    await transition_placement(ctx, db, placement, "ended", SYSTEM, case.reason)
    for subject_type, sid in (("placement", placement.id), ("offboarding_case", case.id)):
        await secure_links.revoke_for_subject(db, subject_type, sid)
    await _close_tasks(db, [i.task_id for i in items if i.task_id])
    emit(
        db,
        PlacementEnded(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            candidate_id=placement.candidate_id,
            end_date=case.last_working_day,
            reason=case.reason,
            worker_type=placement.worker_type or "employee",
            bench_eligible=eligible,
        ),
    )
    return case


async def cancel(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, reason: str) -> OffboardingCase:
    actor = actor_from_ctx(ctx)
    case = await _visible_case(ctx, db, case_id)
    placement = await get_placement(db, case.placement_id, lock=True)
    case = await get_case(db, case_id, lock=True)
    check_transition(case.status, "cancelled", actor.kind)
    if await local_today(db, placement) >= case.last_working_day:
        raise ConflictError("The last working day has passed", code="past_last_working_day")
    case.cancel_reason = reason
    await _transition(db, case, "cancelled", actor)
    await exit_settings.set_end_date(
        db, placement, case.previous_end_date, "cancel_offboarding", case.id, actor
    )
    await transition_placement(ctx, db, placement, "active", SYSTEM, "offboarding_cancelled")
    await _close_tasks(db, [i.task_id for i in await _items(db, case.id) if i.task_id])
    await secure_links.revoke_for_subject(db, "offboarding_case", case.id)
    return case


async def record_feedback(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, answers: dict[str, Any]
) -> OffboardingCase:
    actor = actor_from_ctx(ctx)
    case = await _visible_case(ctx, db, case_id, lock=True)
    row = (
        await db.execute(select(OffboardingFeedback).where(OffboardingFeedback.case_id == case.id))
    ).scalar_one_or_none()
    if row is None:
        db.add(
            OffboardingFeedback(
                tenant_id=case.tenant_id,
                case_id=case.id,
                answers=answers,
                recorded_by_kind=actor.kind,
                recorded_by=actor.id,
            )
        )
    else:
        row.answers = answers
    await db.flush()
    item = next((i for i in await _items(db, case.id) if i.kind == "exit_feedback"), None)
    if item is not None and item.status == "pending" and case.status in CASE_OPEN:
        await _mark_done(db, case, item, "user", actor.id)
        await _settle_status(db, case, await _items(db, case.id))
    return case


async def client_link(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> LinkOut:
    case = await _visible_case(ctx, db, case_id, lock=True)
    if case.status not in CASE_OPEN:
        raise ConflictError("The offboarding case is not open", code="case_not_open")
    await secure_links.revoke_for_subject(db, "offboarding_case", case.id, LINK_PURPOSE)
    actor = actor_from_ctx(ctx)
    link, token = await secure_links.issue(
        db,
        tenant_id=case.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type="offboarding_case",
        subject_id=case.id,
        ttl=LINK_TTL,
        metadata={"placement_id": str(case.placement_id)},
        created_by=actor.id if actor.kind == "user" else None,
    )
    return LinkOut(url=secure_links.link_url(token), expires_at=link.expires_at)


# ---------------------------------------------------------------- reads
async def _case_out(
    db: AsyncSession, case: OffboardingCase, cls: type[OffboardingCaseOut] = OffboardingCaseOut
) -> Any:
    placement = await db.get(Placement, case.placement_id)
    cands, clients = await names(
        db,
        candidate_ids={placement.candidate_id} if placement else set(),
        client_ids={placement.client_id} if placement else set(),
    )
    counts = (
        await db.execute(
            select(func.count(), func.count().filter(OffboardingItem.status == "pending")).where(
                OffboardingItem.case_id == case.id
            )
        )
    ).one()
    base = OffboardingCaseOut.model_validate(case).model_dump()
    base.update(
        placement_human_id=placement.human_id if placement else None,
        candidate_name=cands.get(placement.candidate_id) if placement else None,
        client_name=clients.get(placement.client_id) if placement else None,
        worker_type=placement.worker_type if placement else None,
        items_total=int(counts[0]),
        items_open=int(counts[1]),
    )
    return cls(**base)


async def detail_out(
    db: AsyncSession, case: OffboardingCase, *, compensation: bool = True
) -> OffboardingCaseDetail:
    await db.flush()
    await db.refresh(case)
    out = await _case_out(db, case, OffboardingCaseDetail)
    items = await _items(db, case.id)
    owner_ids = {i.owner_id for i in items if i.owner_id}
    owners: dict[uuid.UUID, str] = {}
    if owner_ids:
        from app.models.identity import User

        for uid, name in (await db.execute(select(User.id, User.name).where(User.id.in_(owner_ids)))).all():
            owners[uid] = name
    out.items = [
        OffboardingItemOut.model_validate(i).model_copy(update={"owner_name": owners.get(i.owner_id)})
        for i in items
    ]
    fb = (
        await db.execute(select(OffboardingFeedback).where(OffboardingFeedback.case_id == case.id))
    ).scalar_one_or_none()
    out.feedback = FeedbackOut.model_validate(fb) if fb else None
    out.settlement = [
        SettlementLine(
            kind=i.kind,
            label=i.label,
            status=i.status,
            auto_verified=i.auto_verified,
            details=i.verifier_details,
            link=SETTLEMENT_KINDS[i.kind].format(pid=case.placement_id),
        )
        for i in items
        if i.kind in SETTLEMENT_KINDS
    ]
    conv = (
        await db.execute(select(Conversion).where(Conversion.offboarding_case_id == case.id))
    ).scalar_one_or_none()
    if conv is not None:
        c = ConversionOut.model_validate(conv)
        out.conversion = c if compensation else c.model_copy(update={"offered_compensation": None})
    return out


async def get_detail(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> OffboardingCase:
    return await _visible_case(ctx, db, case_id)


async def latest_for_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> OffboardingCase | None:
    from app.services.placements.lists import visible

    await visible(ctx, db, placement_id)
    return (
        await db.execute(
            select(OffboardingCase)
            .where(OffboardingCase.placement_id == placement_id)
            .order_by(OffboardingCase.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_cases(ctx: RequestContext, db: AsyncSession, status: str = "open") -> list[OffboardingCaseOut]:
    from app.services.placements.lists import scoped

    stmt = select(OffboardingCase).join(Placement, Placement.id == OffboardingCase.placement_id)
    stmt = scoped(stmt, ctx.principal)
    if status == "open":
        stmt = stmt.where(OffboardingCase.status.in_(CASE_OPEN))
    elif status != "all":
        stmt = stmt.where(OffboardingCase.status == status)
    rows = list((await db.execute(stmt.order_by(OffboardingCase.last_working_day))).scalars())
    return [await _case_out(db, r) for r in rows]


async def exit_facts(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> Any:  # ruff: ignore[unused-function-argument]
    """Read API for FR-99 / FR-176 reports: the latest non-cancelled case of the placement."""
    from app.schemas.placements_exit import ExitFacts

    placement = await get_placement(db, placement_id)
    case = (
        await db.execute(
            select(OffboardingCase)
            .where(OffboardingCase.placement_id == placement_id, OffboardingCase.status != "cancelled")
            .order_by(OffboardingCase.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ExitFacts(
        placement_id=placement_id,
        case_id=case.id if case else None,
        reason=case.reason if case else None,
        last_working_day=case.last_working_day if case else None,
        early_attrition=bool(case.early_attrition) if case else False,
        notice_shortfall_days=case.notice_shortfall_days if case else None,
        recruiter_user_id=placement.recruiter_user_id,
        vendor_id=placement.vendor_id,
    )
