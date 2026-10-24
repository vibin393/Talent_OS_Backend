"""Leave Requests (Epic 26, FR-136, §5.10) on the extended approvals engine (AD-24).

* ``transition_leave`` is the single writer of ``leave_requests.status`` (AD-4); ★ rows are human-only (a
  ``workflow`` / ``ai`` / ``system`` actor gets 409 ``transition_not_allowed``).
* Create (Worker portal or User proxy): eligibility, half-day rule, day count (``calendar.leave_days``),
  overlap
  (409 ``leave_overlap``) and the balance check under the balance-row lock (409 ``balance_insufficient``
  unless
  negatives are allowed down to ``-negative_limit``); unpaid types skip the balance. Success adds to
  ``pending``.
* Steps: the first internal step of ``placement_time_route`` (Account Manager or named User) + a Client
  step when
  the effective rule requires Client approval (``leave_client_mode='approve'`` or the type's Client override).
* Final approve -> ``approved`` + ``request_debit``; reject -> ``rejected``; withdraw -> ``withdrawn``
  (pending
  released). Cancel of ``approved`` -> ``cancellation_requested`` (guard: every leave day's Timesheet must
  still
  be editable: submitted -> 409 ``timesheet_recall_required``, approved -> 409 ``adjustment_required``);
  approve
  -> ``cancelled`` + ``request_credit``; reject -> back to ``approved``.
* Approved leave pre-fills draft Timesheets (``approved_leave`` prefill source); decisions refresh the
  affected
  draft Timesheets in the same transaction (submitted ones are never touched).
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, is_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.time import LeaveRequestDecided
from app.models.candidates import Candidate
from app.models.clients import Client
from app.models.placements import Placement
from app.models.time import LeaveRequest, LeaveType, Timesheet
from app.schemas.approvals import SubjectSummary
from app.schemas.leave import (
    LeaveCalendarDay,
    LeaveCalendarOut,
    LeaveCalendarWorker,
    LeavePreviewOut,
    LeaveRequestIn,
    LeaveRequestOut,
)
from app.schemas.time import PrefillItem
from app.services import approvals
from app.services.approvals import FinalDecision, StepDecision, StepSpec
from app.services.time import accruals, calendar
from app.services.time.accruals import q_days
from app.services.time.common import get_placement, now, tenant_of
from app.services.time.holidays import HolidayDay, day_out, holidays_for, leave_settings_row
from app.services.time.leave_balances import (
    BalanceKey,
    change_pending,
    leave_year_mode,
    lock_balance,
    post_entry,
)
from app.services.time.leave_events import (
    LeaveApproved,
    LeaveCancellationRequested,
    LeaveCancelled,
    LeaveClientNotice,
    LeaveRejected,
    LeaveRequested,
    LeaveWithdrawn,
)
from app.services.time.leave_types import EffectiveRule, client_mode, effective_rule, eligible, get_type

SUBJECT = "leave_request"
ROUTE = "placement_time_route"
OPEN_STATUSES = ("requested", "approved", "cancellation_requested")
REQUESTABLE_PLACEMENT_STATUSES = ("active", "offboarding")
_HALF = Decimal("0.5")


# ================================================================ transitions (AD-4)
@dataclasses.dataclass(frozen=True, slots=True)
class TransitionDef:
    human_only: bool  # ★ rows: approver decisions
    note: str


LEAVE_TRANSITIONS: dict[tuple[str, str], TransitionDef] = {
    ("requested", "approved"): TransitionDef(True, "final approval step approved"),
    ("requested", "rejected"): TransitionDef(True, "rejected by an approver (comment required)"),
    ("requested", "withdrawn"): TransitionDef(False, "withdrawn by the Worker before a decision"),
    ("approved", "cancellation_requested"): TransitionDef(False, "Worker asks to cancel approved leave"),
    ("cancellation_requested", "cancelled"): TransitionDef(True, "cancellation approved"),
    ("cancellation_requested", "approved"): TransitionDef(True, "cancellation rejected"),
}


def transition_not_allowed(from_status: str, to_status: str) -> ConflictError:
    return ConflictError(
        "This leave request cannot move to that status",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status},
    )


def transition_leave(req: LeaveRequest, to_status: str, actor: ActorRef, *, auto: bool = False) -> None:
    """Write ``leave_requests.status`` (its single writer). ``auto``: an engine auto-approval (actor
    ``system``)."""
    definition = LEAVE_TRANSITIONS.get((req.status, to_status))
    if definition is None:
        raise transition_not_allowed(req.status, to_status)
    if definition.human_only and not is_human(actor) and not auto:
        raise transition_not_allowed(req.status, to_status)
    req.status = to_status


# ================================================================ helpers
async def get_request(db: AsyncSession, request_id: uuid.UUID, *, lock: bool = False) -> LeaveRequest:
    stmt = select(LeaveRequest).where(LeaveRequest.id == request_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    req = (await db.execute(stmt)).scalar_one_or_none()
    if req is None:
        raise NotFound("Leave request not found")
    return req


async def work_week(db: AsyncSession, placement: Placement) -> frozenset[int]:
    from app.services.time.configs import latest_config

    cfg = await latest_config(db, placement.id)
    if cfg is not None and cfg.work_days:
        return frozenset(int(d) for d in cfg.work_days)
    settings = await leave_settings_row(db, placement.tenant_id)
    return frozenset(int(d) for d in (settings.default_work_week or calendar.DEFAULT_WORK_WEEK))


@dataclasses.dataclass
class DayCount:
    days: Decimal
    working_dates: list[date]
    holidays: list[HolidayDay]


async def count_days(
    db: AsyncSession,
    placement: Placement,
    start: date,
    end: date,
    start_half: str | None,
    end_half: str | None,
) -> DayCount:
    if end < start:
        raise ValidationFailed("The end date is before the start date", code="dates_invalid")
    if (end - start).days > 366:
        raise ValidationFailed("A leave request can span at most one year", code="dates_invalid")
    week = await work_week(db, placement)
    hols = await holidays_for(db, placement.id, start, end)
    hol_dates = [h.date for h in hols]
    try:
        days = calendar.leave_days(start, end, week, hol_dates, start_half=start_half, end_half=end_half)
    except ValueError as exc:
        raise ValidationFailed(
            "A single-day request cannot take both half days; request a full day instead",
            code="half_day_invalid",
        ) from exc
    return DayCount(days, calendar.leave_working_dates(start, end, week, hol_dates), hols)


async def _placement_for(
    db: AsyncSession, candidate_id: uuid.UUID, placement_id: uuid.UUID | None, on: date
) -> Placement:
    if placement_id is not None:
        p = await get_placement(db, placement_id)
        if p.candidate_id != candidate_id:
            raise NotFound("Placement not found")
        return p
    rows = list(
        (
            await db.execute(
                select(Placement)
                .where(
                    Placement.candidate_id == candidate_id,
                    Placement.status.in_(REQUESTABLE_PLACEMENT_STATUSES),
                )
                .order_by(Placement.created_at.desc())
            )
        ).scalars()
    )
    for p in rows:
        start = p.actual_start_date or p.planned_start_date
        end = p.end_date or p.planned_end_date
        if start <= on and (end is None or on <= end):
            return p
    if rows:
        return rows[0]
    raise ValidationFailed("You have no active placement to request leave on", code="no_active_placement")


def _check_dates(p: Placement, start: date, end: date) -> None:
    if p.status not in REQUESTABLE_PLACEMENT_STATUSES:
        raise ValidationFailed(
            "Leave can be requested only on an active placement", code="placement_not_active"
        )
    p_start = p.actual_start_date or p.planned_start_date
    p_end = p.end_date or p.planned_end_date
    if start < p_start or (p_end is not None and end > p_end):
        raise ValidationFailed(
            "The leave dates must be within the placement dates",
            code="dates_outside_placement",
            details={"start": p_start.isoformat(), "end": p_end.isoformat() if p_end else None},
        )


async def _validated(
    db: AsyncSession, p: Placement, data: LeaveRequestIn
) -> tuple[LeaveType, EffectiveRule, DayCount]:
    _check_dates(p, data.start_date, data.end_date)
    t = await get_type(db, data.leave_type_id)
    if not eligible(t, p.worker_type, p.legal_entity_id):
        raise ValidationFailed(
            "This leave type is not available for this placement", code="leave_type_not_eligible"
        )
    rule = await effective_rule(db, t, p.client_id)
    if (data.start_half or data.end_half) and not rule.allow_half_day:
        raise ValidationFailed("Half days are not allowed for this leave type", code="half_day_not_allowed")
    count = await count_days(db, p, data.start_date, data.end_date, data.start_half, data.end_half)
    if count.days <= 0:
        raise ValidationFailed("The selected dates have no working days", code="no_working_days")
    return t, rule, count


def _insufficient(available: Decimal) -> ConflictError:
    return ConflictError(
        "Not enough leave balance for this request",
        code="balance_insufficient",
        details={"available": str(available)},
    )


def _balance_ok(rule: EffectiveRule, balance: Decimal, pending: Decimal, days: Decimal) -> bool:
    available = balance - pending
    if days <= available:
        return True
    if not rule.allow_negative:
        return False
    if rule.negative_limit is None:
        return True
    return balance - pending - days >= -Decimal(rule.negative_limit)


async def _overlap(
    db: AsyncSession, candidate_id: uuid.UUID, start: date, end: date, exclude: uuid.UUID | None = None
) -> bool:
    stmt = select(LeaveRequest.id).where(
        LeaveRequest.candidate_id == candidate_id,
        LeaveRequest.status.in_(OPEN_STATUSES),
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    if exclude is not None:
        stmt = stmt.where(LeaveRequest.id != exclude)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None


def _key(req: LeaveRequest) -> BalanceKey:
    return BalanceKey(req.candidate_id, req.leave_type_id, req.legal_entity_id, req.leave_year)


# ================================================================ preview / create
async def preview(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: LeaveRequestIn
) -> LeavePreviewOut:
    """Pure day count + balance check (writes nothing but the lazy settings row)."""
    p = await _placement_for(db, candidate_id, data.placement_id, data.start_date)
    try:
        t, rule, count = await _validated(db, p, data)
    except (ValidationFailed, NotFound) as exc:
        return LeavePreviewOut(
            days=Decimal("0.00"),
            working_dates=[],
            holidays=[],
            balance=None,
            pending=None,
            available=None,
            sufficient=False,
            error_code=getattr(exc, "code", "invalid"),
        )
    if not t.paid:
        return LeavePreviewOut(
            days=count.days,
            working_dates=count.working_dates,
            holidays=[day_out(h) for h in count.holidays],
            balance=None,
            pending=None,
            available=None,
            sufficient=True,
        )
    from app.models.time import LeaveBalance

    year = accruals.leave_year_of(data.start_date, await leave_year_mode(db, tenant_of(ctx, db)))
    bal = (
        await db.execute(
            select(LeaveBalance).where(
                LeaveBalance.candidate_id == candidate_id,
                LeaveBalance.leave_type_id == t.id,
                LeaveBalance.legal_entity_id == p.legal_entity_id,
                LeaveBalance.leave_year == year,
            )
        )
    ).scalar_one_or_none()
    balance = Decimal(bal.balance) if bal is not None else Decimal("0.00")
    pending = Decimal(bal.pending) if bal is not None else Decimal("0.00")
    ok = _balance_ok(rule, balance, pending, count.days)
    return LeavePreviewOut(
        days=count.days,
        working_dates=count.working_dates,
        holidays=[day_out(h) for h in count.holidays],
        balance=q_days(balance),
        pending=q_days(pending),
        available=q_days(balance - pending),
        sufficient=ok,
        error_code=None if ok else "balance_insufficient",
    )


async def create_request(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    data: LeaveRequestIn,
    *,
    actor: ActorRef,
    proxy_reason: str | None = None,
) -> LeaveRequest:
    tenant_id = tenant_of(ctx, db)
    p = await _placement_for(db, candidate_id, data.placement_id, data.start_date)
    t, rule, count = await _validated(db, p, data)
    year = accruals.leave_year_of(data.start_date, await leave_year_mode(db, tenant_id))
    key = BalanceKey(candidate_id, t.id, p.legal_entity_id, year)
    # The balance row lock serialises concurrent requests of the same Worker / type (overlap + balance).
    bal = await lock_balance(db, tenant_id, key)
    if await _overlap(db, candidate_id, data.start_date, data.end_date):
        raise ConflictError("You already have leave on some of these dates", code="leave_overlap")
    if t.paid:
        if not _balance_ok(rule, Decimal(bal.balance), Decimal(bal.pending), count.days):
            raise _insufficient(q_days(Decimal(bal.balance) - Decimal(bal.pending)))
        bal.pending = q_days(Decimal(bal.pending) + count.days)
    req = LeaveRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "leave_request", "LV", width=6),
        candidate_id=candidate_id,
        placement_id=p.id,
        client_id=p.client_id,
        legal_entity_id=p.legal_entity_id,
        leave_type_id=t.id,
        leave_year=year,
        start_date=data.start_date,
        end_date=data.end_date,
        start_half=data.start_half,
        end_half=data.end_half,
        days=count.days,
        reason=data.reason or None,
        status="requested",
        requested_by_kind=actor.kind,
        requested_by_id=actor.id,
        proxy_reason=proxy_reason,
    )
    db.add(req)
    await db.flush()
    await _start_approval(ctx, db, req, p, rule, actor)
    emit(
        db,
        LeaveRequested(
            tenant_id=tenant_id, leave_request_id=req.id, placement_id=p.id, candidate_id=candidate_id
        ),
    )
    return req


async def _steps(ctx: RequestContext, db: AsyncSession, p: Placement, rule: EffectiveRule) -> list[StepSpec]:
    internal = await approvals.resolve_route(ctx, db, ROUTE, placement_id=p.id, include_client=False)
    steps = list(internal[:1])
    if rule.requires_client_approval:
        steps.append(
            StepSpec(
                "Client", "client_any", {"client_id": str(p.client_id), "portal_permission": "time:approve"}
            )
        )
    if not steps:
        raise ValidationFailed("This placement has no leave approver", code="approval_route_empty")
    return steps


async def _facts(req: LeaveRequest) -> dict[str, Any]:  # NOSONAR
    return {
        "leave_request_id": str(req.id),
        "placement_id": str(req.placement_id),
        "client_id": str(req.client_id),
        "leave_type_id": str(req.leave_type_id),
        "start_date": req.start_date.isoformat(),
        "end_date": req.end_date.isoformat(),
        "days": str(req.days),
        "phase": "cancellation" if req.status == "cancellation_requested" else "request",
    }


async def _start_approval(
    ctx: RequestContext,
    db: AsyncSession,
    req: LeaveRequest,
    p: Placement,
    rule: EffectiveRule,
    actor: ActorRef,
) -> None:
    steps = await _steps(ctx, db, p, rule)
    result = await approvals.start(
        ctx, db, SUBJECT, req.id, await _facts(req), steps=steps, initiated_by=actor
    )
    req.approval_request_id = result.request_id
    await db.flush()
    if result.status == "auto_approved":
        await _finalise(ctx, db, req, "approved", ActorRef("system", None), None, auto=True)


# ================================================================ withdraw / cancel
async def withdraw(ctx: RequestContext, db: AsyncSession, req: LeaveRequest, actor: ActorRef) -> LeaveRequest:
    if req.status != "requested":
        raise transition_not_allowed(req.status, "withdrawn")
    transition_leave(req, "withdrawn", actor)
    await approvals.cancel_open(ctx, db, SUBJECT, req.id)
    if await _paid(db, req):
        await change_pending(db, req.tenant_id, _key(req), -Decimal(req.days))
    await db.flush()
    emit(db, LeaveWithdrawn(tenant_id=req.tenant_id, leave_request_id=req.id, placement_id=req.placement_id))
    return req


async def cancellation_guard(ctx: RequestContext, db: AsyncSession, req: LeaveRequest) -> None:
    from app.services.time.monitoring import timesheet_state_for

    p = await get_placement(db, req.placement_id)
    count = await count_days(db, p, req.start_date, req.end_date, None, None)
    worst: str | None = None
    for d in count.working_dates:
        state = await timesheet_state_for(ctx, db, req.placement_id, d)
        if state is None or state.status in ("draft", "rejected"):
            continue
        if state.status == "approved":
            worst = "adjustment_required"
            break
        worst = "timesheet_recall_required"
    if worst == "adjustment_required":
        raise ConflictError(
            "The timesheet for these dates is approved; correct it with an adjustment timesheet",
            code="adjustment_required",
        )
    if worst == "timesheet_recall_required":
        raise ConflictError(
            "The timesheet for these dates is submitted; recall it before cancelling the leave",
            code="timesheet_recall_required",
        )


async def request_cancel(
    ctx: RequestContext, db: AsyncSession, req: LeaveRequest, actor: ActorRef
) -> LeaveRequest:
    if req.status != "approved":
        raise transition_not_allowed(req.status, "cancellation_requested")
    await cancellation_guard(ctx, db, req)
    transition_leave(req, "cancellation_requested", actor)
    await db.flush()
    p = await get_placement(db, req.placement_id)
    rule = await effective_rule(db, await get_type(db, req.leave_type_id), p.client_id)
    emit(
        db,
        LeaveCancellationRequested(
            tenant_id=req.tenant_id, leave_request_id=req.id, placement_id=req.placement_id
        ),
    )
    await _start_approval(ctx, db, req, p, rule, actor)
    return req


# ================================================================ approval callbacks (synchronous, AD-24)
async def _paid(db: AsyncSession, req: LeaveRequest) -> bool:
    return bool(
        (await db.execute(select(LeaveType.paid).where(LeaveType.id == req.leave_type_id))).scalar_one()
    )


async def _finalise(
    ctx: RequestContext,
    db: AsyncSession,
    req: LeaveRequest,
    outcome: str,
    actor: ActorRef,
    comment: str | None,
    *,
    auto: bool = False,
) -> None:
    paid = await _paid(db, req)
    key = _key(req)
    days = Decimal(req.days)
    if req.status == "requested":
        if outcome == "approved":
            transition_leave(req, "approved", actor, auto=auto)
            if paid:
                bal = await change_pending(db, req.tenant_id, key, -days)
                await post_entry(
                    db,
                    req.tenant_id,
                    key,
                    kind="request_debit",
                    days=-days,
                    effective_date=req.start_date,
                    source_ref=f"request:{req.id}",
                    actor=actor,
                    row=bal,
                )
            emit(
                db,
                LeaveApproved(
                    tenant_id=req.tenant_id,
                    leave_request_id=req.id,
                    placement_id=req.placement_id,
                    start_date=req.start_date,
                    end_date=req.end_date,
                ),
            )
            if await client_mode(db, req.client_id) == "notify":
                emit(
                    db,
                    LeaveClientNotice(
                        tenant_id=req.tenant_id, leave_request_id=req.id, client_id=req.client_id
                    ),
                )
        else:
            transition_leave(req, "rejected", actor, auto=auto)
            if paid:
                await change_pending(db, req.tenant_id, key, -days)
            emit(
                db,
                LeaveRejected(
                    tenant_id=req.tenant_id, leave_request_id=req.id, placement_id=req.placement_id
                ),
            )
        result = "approved" if outcome == "approved" else "rejected"
    elif req.status == "cancellation_requested":
        if outcome == "approved":
            transition_leave(req, "cancelled", actor, auto=auto)
            if paid:
                await post_entry(
                    db,
                    req.tenant_id,
                    key,
                    kind="request_credit",
                    days=days,
                    effective_date=req.start_date,
                    source_ref=f"request-cancel:{req.id}",
                    actor=actor,
                )
            emit(
                db,
                LeaveCancelled(
                    tenant_id=req.tenant_id,
                    leave_request_id=req.id,
                    placement_id=req.placement_id,
                    start_date=req.start_date,
                    end_date=req.end_date,
                ),
            )
            result = "cancelled"
        else:
            transition_leave(req, "approved", actor, auto=auto)
            result = "cancellation_rejected"
    else:
        return
    req.decided_at = now()
    req.decision_comment = comment
    await db.flush()
    emit(
        db,
        LeaveRequestDecided(
            tenant_id=req.tenant_id, leave_request_id=req.id, candidate_id=req.candidate_id, outcome=result
        ),
    )
    await refresh_drafts(ctx, db, req.placement_id, req.start_date, req.end_date)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: FinalDecision) -> None:
    req = await get_request(db, decision.subject_id, lock=True)
    if req.status not in ("requested", "cancellation_requested"):
        return  # replay: already decided
    if decision.request_id is not None and req.approval_request_id not in (None, decision.request_id):
        return  # a superseded request
    approved = decision.outcome in ("approved", "auto_approved")
    actor = decision.actor or ActorRef("system", None)
    await _finalise(
        ctx,
        db,
        req,
        "approved" if approved else "rejected",
        actor,
        decision.comment,
        auto=decision.actor is None,
    )


async def on_step_decided(ctx: RequestContext, db: AsyncSession, decision: StepDecision) -> None:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return None


async def describe(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SubjectSummary]:
    del ctx
    rows = (
        await db.execute(
            select(
                LeaveRequest.id,
                LeaveRequest.human_id,
                LeaveRequest.start_date,
                LeaveRequest.end_date,
                LeaveRequest.days,
                LeaveRequest.status,
                Candidate.full_name,
                LeaveType.name,
            )
            .join(Candidate, Candidate.id == LeaveRequest.candidate_id, isouter=True)
            .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id, isouter=True)
            .where(LeaveRequest.id.in_(ids))
        )
    ).all()
    out: dict[uuid.UUID, SubjectSummary] = {}
    for r in rows:
        prefix = "Cancel leave" if r.status == "cancellation_requested" else "Leave"
        out[r.id] = SubjectSummary(
            label=f"{prefix} {r.human_id}",
            title=f"{r.full_name or 'Worker'} · {r.name or 'Leave'} · {r.start_date.isoformat()} to "
            f"{r.end_date.isoformat()} ({r.days} days)",
        )
    return out


# ================================================================ Timesheet prefill (A6)
async def refresh_drafts(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, start: date, end: date
) -> int:
    """Re-run the prefill of the placement's draft regular Timesheets overlapping ``[start, end]``
    (idempotent;
    submitted / approved Timesheets are never touched)."""
    from app.services.time.configs import config_on
    from app.services.time.prefill import apply_prefill

    sheets = list(
        (
            await db.execute(
                select(Timesheet).where(
                    Timesheet.placement_id == placement_id,
                    Timesheet.kind == "regular",
                    Timesheet.status == "draft",
                    Timesheet.period_start <= end,
                    Timesheet.period_end >= start,
                )
            )
        ).scalars()
    )
    if not sheets:
        return 0
    placement = await get_placement(db, placement_id)
    n = 0
    for ts in sheets:
        cfg = await config_on(ctx, db, placement, ts.period_start)
        n += await apply_prefill(db, ts, cfg)
    return n


async def leave_prefill(
    db: AsyncSession, placement_id: uuid.UUID, start: date, end: date
) -> list[PrefillItem]:
    from app.services.time.configs import latest_config

    rows = list(
        (
            await db.execute(
                select(LeaveRequest, LeaveType)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .where(
                    LeaveRequest.placement_id == placement_id,
                    LeaveRequest.status.in_(("approved", "cancellation_requested")),
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                )
            )
        ).all()
    )
    if not rows:
        return []
    placement = await get_placement(db, placement_id)
    cfg = await latest_config(db, placement_id)
    hours = Decimal(cfg.hours_per_day) if cfg is not None and cfg.hours_per_day else Decimal("8.00")
    week = await work_week(db, placement)
    out: list[PrefillItem] = []
    for req, t in rows:
        hol = [h.date for h in await holidays_for(db, placement_id, req.start_date, req.end_date)]
        for d in calendar.leave_working_dates(req.start_date, req.end_date, week, hol):
            if not start <= d <= end:
                continue
            half = (d == req.start_date and req.start_half == "pm") or (
                d == req.end_date and req.end_half == "am"
            )
            h = (hours * _HALF if half else hours).quantize(Decimal("0.01"))
            out.append(
                PrefillItem(date=d, category=t.timesheet_category, hours=h, leave_type_id=t.id, label=t.name)
            )
    return out


# ================================================================ reads
def to_out(
    req: LeaveRequest,
    *,
    worker_name: str | None = None,
    client_name: str | None = None,
    type_name: str | None = None,
) -> LeaveRequestOut:
    return LeaveRequestOut(
        id=req.id,
        human_id=req.human_id,
        candidate_id=req.candidate_id,
        worker_name=worker_name,
        placement_id=req.placement_id,
        client_id=req.client_id,
        client_name=client_name,
        legal_entity_id=req.legal_entity_id,
        leave_type_id=req.leave_type_id,
        leave_type_name=type_name,
        leave_year=req.leave_year,
        start_date=req.start_date,
        end_date=req.end_date,
        start_half=req.start_half,
        end_half=req.end_half,
        days=req.days,
        reason=req.reason,
        status=req.status,
        approval_request_id=req.approval_request_id,
        requested_by_kind=req.requested_by_kind,
        proxy_reason=req.proxy_reason,
        decided_at=req.decided_at,
        decision_comment=req.decision_comment,
        created_at=req.created_at,
    )


def _joined() -> Any:
    return (
        select(LeaveRequest, Candidate.full_name, Client.name, LeaveType.name)
        .join(Candidate, Candidate.id == LeaveRequest.candidate_id, isouter=True)
        .join(Client, Client.id == LeaveRequest.client_id, isouter=True)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id, isouter=True)
    )


async def list_requests(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
) -> list[LeaveRequestOut]:
    del ctx
    stmt = _joined()
    if candidate_id is not None:
        stmt = stmt.where(LeaveRequest.candidate_id == candidate_id)
    if placement_id is not None:
        stmt = stmt.where(LeaveRequest.placement_id == placement_id)
    if client_id is not None:
        stmt = stmt.where(LeaveRequest.client_id == client_id)
    if status:
        stmt = stmt.where(LeaveRequest.status.in_(status.split(",")))
    if start is not None:
        stmt = stmt.where(LeaveRequest.end_date >= start)
    if end is not None:
        stmt = stmt.where(LeaveRequest.start_date <= end)
    rows = (
        await db.execute(
            stmt.order_by(LeaveRequest.start_date.desc(), LeaveRequest.created_at.desc()).limit(limit)
        )
    ).all()
    return [to_out(r[0], worker_name=r[1], client_name=r[2], type_name=r[3]) for r in rows]


async def get_out(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> LeaveRequestOut:
    del ctx
    row = (await db.execute(_joined().where(LeaveRequest.id == request_id))).first()
    if row is None:
        raise NotFound("Leave request not found")
    return to_out(row[0], worker_name=row[1], client_name=row[2], type_name=row[3])


# ================================================================ team calendar (A7)
def expand(req: LeaveRequest, working: list[date], *, with_type: str | None) -> list[LeaveCalendarDay]:
    out: list[LeaveCalendarDay] = []
    status = "approved" if req.status in ("approved", "cancellation_requested") else "requested"
    for d in working:
        half = None
        if d == req.start_date and req.start_half == "pm":
            half = "pm"
        elif d == req.end_date and req.end_half == "am":
            half = "am"
        out.append(LeaveCalendarDay(date=d, half=half, status=status, leave_type_name=with_type))
    return out


async def calendar_rows(
    db: AsyncSession,
    *,
    start: date,
    end: date,
    statuses: tuple[str, ...],
    client_id: uuid.UUID | None = None,
    placement_ids: frozenset[uuid.UUID] | None = None,
) -> list[tuple[LeaveRequest, str | None, str | None, list[date]]]:
    stmt = (
        select(LeaveRequest, Candidate.full_name, LeaveType.name)
        .join(Candidate, Candidate.id == LeaveRequest.candidate_id, isouter=True)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id, isouter=True)
        .where(
            LeaveRequest.status.in_(statuses), LeaveRequest.start_date <= end, LeaveRequest.end_date >= start
        )
    )
    if client_id is not None:
        stmt = stmt.where(LeaveRequest.client_id == client_id)
    if placement_ids is not None:
        if not placement_ids:
            return []
        stmt = stmt.where(LeaveRequest.placement_id.in_(placement_ids))
    out = []
    weeks: dict[uuid.UUID, frozenset[int]] = {}
    for req, name, type_name in (
        await db.execute(stmt.order_by(Candidate.full_name, LeaveRequest.start_date))
    ).all():
        if req.placement_id not in weeks:
            weeks[req.placement_id] = await work_week(db, await get_placement(db, req.placement_id))
        hol = [h.date for h in await holidays_for(db, req.placement_id, req.start_date, req.end_date)]
        working = [
            d
            for d in calendar.leave_working_dates(req.start_date, req.end_date, weeks[req.placement_id], hol)
            if start <= d <= end
        ]
        out.append((req, name, type_name, working))
    return out


async def team_calendar(
    ctx: RequestContext, db: AsyncSession, *, start: date, end: date, client_id: uuid.UUID | None = None
) -> LeaveCalendarOut:
    del ctx
    if end < start or (end - start).days > 92:
        raise ValidationFailed("Choose a range of at most three months", code="range_invalid")
    rows = await calendar_rows(db, start=start, end=end, statuses=OPEN_STATUSES, client_id=client_id)
    workers: dict[uuid.UUID, LeaveCalendarWorker] = {}
    for req, name, type_name, working in rows:
        w = workers.setdefault(
            req.placement_id,
            LeaveCalendarWorker(
                candidate_id=req.candidate_id,
                worker_name=name,
                placement_id=req.placement_id,
                client_id=req.client_id,
                days=[],
            ),
        )
        w.days.extend(expand(req, working, with_type=type_name))
    holidays = []
    seen: set[tuple[date, str]] = set()
    placement_ids = {r[0].placement_id for r in rows}
    if client_id is not None and not placement_ids:
        placement_ids = set(
            (
                await db.execute(
                    select(Placement.id)
                    .where(
                        Placement.client_id == client_id, Placement.status.in_(REQUESTABLE_PLACEMENT_STATUSES)
                    )
                    .limit(1)
                )
            ).scalars()
        )
    for pid in placement_ids:
        for h in await holidays_for(db, pid, start, end):
            if (h.date, h.name) not in seen:
                seen.add((h.date, h.name))
                holidays.append(day_out(h))
    holidays.sort(key=lambda h: h.date)
    return LeaveCalendarOut(start=start, end=end, workers=list(workers.values()), holidays=holidays)


async def worker_requests(db: AsyncSession, candidate_id: uuid.UUID) -> list[tuple[LeaveRequest, str | None]]:
    return [
        (r, n)
        for r, n in (
            await db.execute(
                select(LeaveRequest, LeaveType.name)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id, isouter=True)
                .where(LeaveRequest.candidate_id == candidate_id)
                .order_by(LeaveRequest.start_date.desc())
            )
        ).all()
    ]
