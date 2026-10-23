"""Timesheets and entries (FR-129/130/132): period creation, entry validation and upsert, OT/DT split on
submit,
recall, reopen and the read projections. ``transition_timesheet`` is the only writer of ``timesheets.status``
(AD-4); starred approver transitions are human-only.

Entry storage: while editable (``draft`` / ``rejected``) a day's worked hours sit on the ``regular`` row
(``hours == entered_hours``); ``submit`` writes the split (``regular`` / ``ot`` / ``double_time``) *before*
the
status leaves ``draft`` (the entry immutability trigger only lets editable parents change), and any return
to an
editable status collapses it again.
"""

import dataclasses
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx, human_only_action, is_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.core.money import MONEY_CONTEXT, quantize_hours
from app.core.sequences import next_human_id
from app.events.time import TimesheetSubmitted
from app.models.approvals import ApprovalDecision, ApprovalRequest
from app.models.placements import Placement
from app.models.postings import BillingPosting, PayablePosting
from app.models.time import Timesheet, TimesheetAdjustmentEntry, TimesheetConfig, TimesheetEntry
from app.schemas.time import (
    COMPUTED_CATEGORIES,
    TIME_CATEGORIES,
    AdjustmentEntryOut,
    ApprovalStepOut,
    CategoryTotal,
    DayEntryIn,
    DaySplitOut,
    EntryOut,
    LockFlags,
    SplitPreviewOut,
    TimesheetDetailOut,
    TimesheetSummaryOut,
)
from app.services.time import calendar, overtime, prefill
from app.services.time.common import (
    DAY_MAX,
    ZERO,
    fmt_time,
    get_placement,
    invalid,
    not_editable,
    now,
    parse_hours,
)
from app.services.time.common import get_timesheet as get_timesheet
from app.services.time.configs import config_on, config_version
from app.services.time.events import TimesheetRecalled, TimesheetReopened

EDITABLE = ("draft", "rejected")
IN_FLIGHT = ("submitted", "in_approval")
ACTIVE_PLACEMENT_STATUSES = ("active", "offboarding", "ended")
SOURCE_KIND_ENTRY = "timesheet_entry"
SOURCE_KIND_ADJUSTMENT = "adjustment_entry"


# ================================================================ transitions (AD-4)
@dataclasses.dataclass(frozen=True, slots=True)
class TransitionDef:
    human_only: bool  # ★ rows: approver decisions / reopen
    note: str


TIMESHEET_TRANSITIONS: dict[tuple[str, str], TransitionDef] = {
    ("draft", "submitted"): TransitionDef(False, "submit (Worker, proxy or import)"),
    ("rejected", "submitted"): TransitionDef(False, "resubmit after rejection"),
    ("submitted", "draft"): TransitionDef(False, "recall before any decision"),
    ("submitted", "in_approval"): TransitionDef(True, "first step approved"),
    ("submitted", "approved"): TransitionDef(True, "single / last step approved"),
    ("in_approval", "approved"): TransitionDef(True, "final step approved"),
    ("submitted", "rejected"): TransitionDef(True, "rejected by an approver"),
    ("in_approval", "rejected"): TransitionDef(True, "rejected by an approver"),
    ("approved", "draft"): TransitionDef(True, "reopen by Finance (no active posting)"),
}


def transition_not_allowed(from_status: str, to_status: str) -> ConflictError:
    return ConflictError(
        "This timesheet cannot move to that status",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status},
    )


def transition_timesheet(ts: Timesheet, to_status: str, actor: ActorRef, *, auto: bool = False) -> None:
    """Write ``status`` -- the single writer of ``timesheets.status``. ``auto``: the empty-route
    auto-approval (actor ``system``)."""
    definition = TIMESHEET_TRANSITIONS.get((ts.status, to_status))
    if definition is None:
        raise transition_not_allowed(ts.status, to_status)
    if (
        definition.human_only
        and not is_human(actor)
        and not (auto and (ts.status, to_status) == ("submitted", "approved"))
    ):
        raise human_only_action()
    ts.status = to_status
    ts.lock_version = (ts.lock_version or 0) + 1


# ================================================================ period timesheets
async def _new_human_id(db: AsyncSession) -> str:
    return await next_human_id(db, "timesheet", "TS", width=6)


async def create_period_timesheets(
    ctx: RequestContext, db: AsyncSession, placement: Placement, *, up_to: date | None = None
) -> list[Timesheet]:
    """Ensure a regular Timesheet for every period overlapping ``[actual_start_date, min(end_date, up_to)]``
    (idempotent through the UNIQUE). Returns the Timesheets created now."""
    if placement.status not in ACTIVE_PLACEMENT_STATUSES or placement.actual_start_date is None:
        return []
    from app.services.time.configs import ensure_config

    cfg_latest = await ensure_config(ctx, db, placement)
    today = up_to or date.today()
    last = min(d for d in (placement.end_date, today) if d is not None)
    if last < placement.actual_start_date:
        return []
    existing = set(
        (
            await db.execute(
                select(Timesheet.period_start).where(
                    Timesheet.placement_id == placement.id, Timesheet.kind == "regular"
                )
            )
        ).scalars()
    )
    created: list[Timesheet] = []
    for start, end in calendar.periods_between(
        cfg_latest.period_kind, cfg_latest.week_start, placement.actual_start_date, last
    ):
        if start in existing:
            continue
        cfg = await config_on(ctx, db, placement, start)
        ts = Timesheet(
            id=uuid.uuid4(),
            tenant_id=placement.tenant_id,
            human_id=await _new_human_id(db),
            placement_id=placement.id,
            candidate_id=placement.candidate_id,
            client_id=placement.client_id,
            vendor_id=placement.vendor_id,
            legal_entity_id=placement.legal_entity_id,
            kind="regular",
            adjustment_seq=0,
            period_start=start,
            period_end=end,
            status="draft",
            config_version=cfg.version,
        )
        try:
            async with db.begin_nested():
                db.add(ts)
                await db.flush()
        except IntegrityError:
            continue
        await prefill.apply_prefill(db, ts, cfg)
        created.append(ts)
    return created


# ================================================================ entry validation / upsert
@dataclasses.dataclass
class _Cell:
    date: date
    category: str
    hours: Decimal
    entered: Decimal | None
    start: time | None = None
    end: time | None = None
    break_minutes: int | None = None
    note: str | None = None


def _parse_hhmm(raw: str) -> time | None:
    try:
        hh, mm = raw.strip().split(":")
        if len(mm) != 2:
            return None
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def span_hours(spans: Sequence[Any]) -> tuple[Decimal | None, time | None, time | None, int]:
    """Hours of a day's spans from the **day's total minutes** (three 20-minute spans = 1.00, not 0.99)."""
    total = 0
    breaks = 0
    first: time | None = None
    last: time | None = None
    for span in spans:
        start, end = _parse_hhmm(span.start), _parse_hhmm(span.end)
        if start is None or end is None or end <= start:
            return None, None, None, 0
        minutes = (
            (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute) - int(span.break_minutes or 0)
        )
        if minutes < 0:
            return None, None, None, 0
        total += minutes
        breaks += int(span.break_minutes or 0)
        first = start if first is None or start < first else first
        last = end if last is None or end > last else last
    hours = quantize_hours(MONEY_CONTEXT.divide(Decimal(total), Decimal(60)))
    return hours, first, last, breaks


def _cell_hours(day: DayEntryIn, cfg: TimesheetConfig) -> tuple[Decimal | None, str | None, Any]:
    """(hours, error code, extra) for one input cell; ``hours`` None with no error = delete."""
    if cfg.entry_mode == "start_end" and day.category == "regular" and day.spans is not None:
        if not day.spans:
            return None, None, None
        hours, first, last, breaks = span_hours(day.spans)
        if hours is None:
            return None, "bad_span", None
        return hours, None, (first, last, breaks)
    if cfg.entry_mode == "days" and day.category == "regular" and day.days is not None:
        raw = day.days.strip()
        if raw in ("", "0"):
            return None, None, None
        days = parse_hours(raw)
        if days is None:
            return None, "bad_decimal", None
        return quantize_hours(MONEY_CONTEXT.multiply(days, Decimal(cfg.hours_per_day))), None, None
    raw = (day.hours or "").strip()
    if raw == "" or (parse_hours(raw) is not None and parse_hours(raw) == 0):
        return None, None, None
    hours = parse_hours(raw)
    if hours is None:
        return None, "bad_decimal", None
    return hours, None, None


def _entry_error(rows: list[dict[str, Any]]) -> ValidationFailed:
    return ValidationFailed("Some entries are not valid", code="entry_invalid", details={"rows": rows})


async def upsert_entries(
    ctx: RequestContext, db: AsyncSession, ts: Timesheet, days: Sequence[DayEntryIn], *, proxy: bool = False
) -> None:
    """Validate and write cells of an editable regular Timesheet (one INSERT ... ON CONFLICT statement)."""
    del ctx
    if ts.status not in EDITABLE:
        raise not_editable(ts.status)
    if ts.kind != "regular":
        raise invalid("adjustment_entries_only", "Adjustment timesheets take signed deltas")
    placement = await get_placement(db, ts.placement_id)
    cfg = await config_version(db, ts.placement_id, ts.config_version)
    enabled = set(cfg.enabled_categories or [])
    current = {
        (e.entry_date, e.category): e
        for e in (
            await db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id))
        ).scalars()
    }
    errors: list[dict[str, Any]] = []
    writes: dict[tuple[date, str], _Cell] = {}
    deletes: set[tuple[date, str]] = set()
    lo = max(ts.period_start, placement.actual_start_date or ts.period_start)
    hi = min(d for d in (ts.period_end, placement.end_date) if d is not None)
    for day in days:
        key = (day.date, day.category)

        def err(code: str, d: DayEntryIn = day) -> None:
            errors.append({"date": d.date.isoformat(), "category": d.category, "code": code})

        if not ts.period_start <= day.date <= ts.period_end:
            err("outside_period")
            continue
        if not lo <= day.date <= hi:
            err("outside_placement_dates")
            continue
        if day.category in COMPUTED_CATEGORIES:
            err("computed_category")
            continue
        if day.category not in enabled:
            err("category_disabled")
            continue
        hours, code, extra = _cell_hours(day, cfg)
        if code is not None:
            err(code)
            continue
        existing = current.get(key)
        if hours is None:
            if existing is not None and existing.prefilled and not existing.prefill_deletable and not proxy:
                err("prefill_locked")
                continue
            deletes.add(key)
            writes.pop(key, None)
            continue
        if hours > DAY_MAX:
            err("over_24h")
            continue
        cell = _Cell(
            day.date, day.category, hours, hours if day.category == "regular" else None, note=day.note
        )
        if extra is not None:
            cell.start, cell.end, cell.break_minutes = extra
        writes[key] = cell
        deletes.discard(key)
    # per-day totals after the change
    totals: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for key, e in current.items():  # type: ignore[assignment]
        if key in deletes or key in writes:
            continue
        totals[key[0]] += (
            e.entered_hours if (e.category == "regular" and e.entered_hours is not None) else e.hours
        )
    for key, cell in writes.items():  # type: ignore[assignment]
        totals[key[0]] += cell.hours
    cap = min(Decimal(cfg.daily_cap), DAY_MAX)
    for d, total in sorted(totals.items()):
        if total > DAY_MAX:
            errors.append({"date": d.isoformat(), "category": None, "code": "over_24h"})
        elif total > cap:
            errors.append({"date": d.isoformat(), "category": None, "code": "over_daily_cap"})
    if errors:
        raise _entry_error(errors)
    if deletes:
        for d, cat in deletes:
            await db.execute(
                delete(TimesheetEntry).where(
                    TimesheetEntry.timesheet_id == ts.id,
                    TimesheetEntry.entry_date == d,
                    TimesheetEntry.category == cat,
                )
            )
    if writes:
        rows = [
            {
                "id": uuid.uuid4(),
                "tenant_id": ts.tenant_id,
                "timesheet_id": ts.id,
                "entry_date": c.date,
                "category": c.category,
                "hours": c.hours,
                "entered_hours": c.entered,
                "start_time": c.start,
                "end_time": c.end,
                "break_minutes": c.break_minutes,
                "note": c.note,
                "prefilled": False,
                "prefill_deletable": False,
                "is_proxy": proxy,
            }
            for c in writes.values()
        ]
        stmt = pg_insert(TimesheetEntry).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["timesheet_id", "entry_date", "category"],
            set_={
                "hours": stmt.excluded.hours,
                "entered_hours": stmt.excluded.entered_hours,
                "start_time": stmt.excluded.start_time,
                "end_time": stmt.excluded.end_time,
                "break_minutes": stmt.excluded.break_minutes,
                "note": stmt.excluded.note,
                "prefilled": False,
                "prefill_deletable": False,
                "holiday_id": None,
                "leave_type_id": None,
                "is_proxy": stmt.excluded.is_proxy,
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)
    ts.lock_version = (ts.lock_version or 0) + 1
    await db.flush()


# ================================================================ split
async def _entries(db: AsyncSession, timesheet_id: uuid.UUID) -> list[TimesheetEntry]:
    return list(
        (
            await db.execute(
                select(TimesheetEntry)
                .where(TimesheetEntry.timesheet_id == timesheet_id)
                .order_by(TimesheetEntry.entry_date, TimesheetEntry.category)
            )
        ).scalars()
    )


def _rules(cfg: TimesheetConfig) -> overtime.OtRules:
    enabled = set(cfg.enabled_categories or [])
    return overtime.OtRules(
        ot_daily=cfg.ot_daily_threshold if "ot" in enabled else None,
        dt_daily=cfg.dt_daily_threshold if "double_time" in enabled else None,
        ot_weekly=cfg.ot_weekly_threshold if "ot" in enabled else None,
    )


async def _prior_regular(db: AsyncSession, ts: Timesheet, week_start: int) -> dict[date, Decimal]:
    """Regular hours already counted in a week that began in an earlier period (submitted Timesheets only)."""
    first_week = overtime.week_of(ts.period_start, week_start)
    if first_week >= ts.period_start:
        return {}
    total = (
        await db.execute(
            select(func.coalesce(func.sum(TimesheetEntry.hours), 0))
            .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
            .where(
                Timesheet.placement_id == ts.placement_id,
                Timesheet.kind == "regular",
                Timesheet.status.in_(("submitted", "in_approval", "approved")),
                TimesheetEntry.category == "regular",
                TimesheetEntry.entry_date >= first_week,
                TimesheetEntry.entry_date < ts.period_start,
            )
        )
    ).scalar_one()
    return {first_week: Decimal(total)} if total else {}


async def compute_split(db: AsyncSession, ts: Timesheet, cfg: TimesheetConfig) -> list[overtime.DaySplit]:
    worked: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for e in await _entries(db, ts.id):
        if e.category == "regular":
            worked[e.entry_date] += e.entered_hours if e.entered_hours is not None else e.hours
    days = [overtime.DayInput(d, h) for d, h in sorted(worked.items())]
    return overtime.split(days, _rules(cfg), cfg.week_start, await _prior_regular(db, ts, cfg.week_start))


def _totals(rows: Iterable[tuple[str, Decimal]]) -> list[CategoryTotal]:
    sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for cat, hours in rows:
        sums[cat] += hours
    return [CategoryTotal(category=c, hours=sums[c]) for c in TIME_CATEGORIES if c in sums]


async def split_preview(db: AsyncSession, ts: Timesheet) -> SplitPreviewOut:
    cfg = await config_version(db, ts.placement_id, ts.config_version)
    if ts.status in EDITABLE:
        splits = await compute_split(db, ts, cfg)
    else:
        by_day: dict[date, dict[str, Decimal]] = defaultdict(dict)
        for e in await _entries(db, ts.id):
            if e.category in ("regular", "ot", "double_time"):
                by_day[e.entry_date][e.category] = e.hours
        splits = [
            overtime.DaySplit(
                d,
                sum(v.values(), ZERO),
                v.get("regular", ZERO),
                v.get("ot", ZERO),
                v.get("double_time", ZERO),
            )
            for d, v in sorted(by_day.items())
        ]
    fixed = [
        (e.category, e.hours)
        for e in await _entries(db, ts.id)
        if e.category not in ("regular", "ot", "double_time")
    ]
    rows = [("regular", s.regular) for s in splits] + [("ot", s.ot) for s in splits if s.ot]
    rows += [("double_time", s.double_time) for s in splits if s.double_time] + fixed
    return SplitPreviewOut(
        days=[
            DaySplitOut(date=s.date, worked=s.worked, regular=s.regular, ot=s.ot, double_time=s.double_time)
            for s in splits
        ],
        totals=_totals(rows),
    )


async def _write_split(db: AsyncSession, ts: Timesheet, splits: Sequence[overtime.DaySplit]) -> None:
    """Replace the computed rows (parent still editable, so the immutability trigger allows it)."""
    await db.execute(
        delete(TimesheetEntry).where(
            TimesheetEntry.timesheet_id == ts.id, TimesheetEntry.category.in_(tuple(COMPUTED_CATEGORIES))
        )
    )
    regular = {
        e.entry_date: e
        for e in (
            await db.execute(
                select(TimesheetEntry).where(
                    TimesheetEntry.timesheet_id == ts.id, TimesheetEntry.category == "regular"
                )
            )
        ).scalars()
    }
    for s in splits:
        row = regular.get(s.date)
        if row is not None:
            row.entered_hours = s.worked
            row.hours = s.regular
        for cat, hours in (("ot", s.ot), ("double_time", s.double_time)):
            if hours > 0:
                db.add(
                    TimesheetEntry(
                        id=uuid.uuid4(),
                        tenant_id=ts.tenant_id,
                        timesheet_id=ts.id,
                        entry_date=s.date,
                        category=cat,
                        hours=hours,
                        is_proxy=bool(row.is_proxy) if row is not None else False,
                    )
                )
    await db.flush()


async def collapse_split(db: AsyncSession, ts: Timesheet) -> None:
    """Back to editable: computed rows go, the regular row holds the worked hours again."""
    if ts.kind != "regular":
        return
    await db.execute(
        delete(TimesheetEntry).where(
            TimesheetEntry.timesheet_id == ts.id, TimesheetEntry.category.in_(tuple(COMPUTED_CATEGORIES))
        )
    )
    for row in (
        await db.execute(
            select(TimesheetEntry).where(
                TimesheetEntry.timesheet_id == ts.id, TimesheetEntry.category == "regular"
            )
        )
    ).scalars():
        if row.entered_hours is not None:
            row.hours = row.entered_hours
    await db.flush()


# ================================================================ submit / recall / reopen
async def _total_hours(db: AsyncSession, ts: Timesheet) -> Decimal:
    if ts.kind == "adjustment":
        value = (
            await db.execute(
                select(func.coalesce(func.sum(TimesheetAdjustmentEntry.delta_hours), 0)).where(
                    TimesheetAdjustmentEntry.timesheet_id == ts.id
                )
            )
        ).scalar_one()
    else:
        value = (
            await db.execute(
                select(func.coalesce(func.sum(TimesheetEntry.hours), 0)).where(
                    TimesheetEntry.timesheet_id == ts.id
                )
            )
        ).scalar_one()
    return Decimal(value).quantize(Decimal("0.01"))


async def submit(
    ctx: RequestContext,
    db: AsyncSession,
    ts: Timesheet,
    *,
    confirm_zero: bool = False,
    proxy_reason: str | None = None,
    actor: ActorRef | None = None,
) -> Timesheet:
    from app.services.time import approval

    actor = actor or actor_from_ctx(ctx)
    if ts.status not in EDITABLE:
        raise not_editable(ts.status)
    if ts.kind == "adjustment":
        count = (
            await db.execute(
                select(func.count())
                .select_from(TimesheetAdjustmentEntry)
                .where(TimesheetAdjustmentEntry.timesheet_id == ts.id)
            )
        ).scalar_one()
        if not count:
            raise invalid("adjustment_empty", "Add at least one adjustment line")
    else:
        if await _total_hours(db, ts) == 0 and not confirm_zero:
            raise invalid(
                "zero_hours_confirm_required", "Confirm that you are submitting a timesheet with no hours"
            )
        cfg = await config_version(db, ts.placement_id, ts.config_version)
        await _write_split(db, ts, await compute_split(db, ts, cfg))
    transition_timesheet(ts, "submitted", actor)
    ts.submitted_at = now()
    ts.submitted_by_kind = actor.kind
    ts.submitted_by_id = actor.id
    ts.rejection_comment = None
    ts.rejected_step = None
    if proxy_reason:
        ts.proxy_reason = proxy_reason
    await db.flush()
    emit(
        db,
        TimesheetSubmitted(
            tenant_id=ts.tenant_id,
            timesheet_id=ts.id,
            placement_id=ts.placement_id,
            period_start=ts.period_start,
            period_end=ts.period_end,
        ),
    )
    await approval.start_route(ctx, db, ts, actor)
    return ts


async def _decided_count(db: AsyncSession, request_id: uuid.UUID | None) -> int:
    if request_id is None:
        return 0
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ApprovalDecision)
                .where(
                    ApprovalDecision.request_id == request_id,
                    ApprovalDecision.outcome.in_(("approved", "rejected", "changes_requested")),
                )
            )
        ).scalar_one()
    )


async def recall(
    ctx: RequestContext, db: AsyncSession, ts: Timesheet, actor: ActorRef | None = None
) -> Timesheet:
    from app.services import approvals

    actor = actor or actor_from_ctx(ctx)
    if ts.status != "submitted" or await _decided_count(db, ts.approval_request_id):
        raise transition_not_allowed(ts.status, "draft")
    await approvals.cancel_open(ctx, db, "timesheet", ts.id)
    transition_timesheet(ts, "draft", actor)
    ts.submitted_at = None
    await db.flush()
    await collapse_split(db, ts)
    emit(db, TimesheetRecalled(tenant_id=ts.tenant_id, timesheet_id=ts.id, placement_id=ts.placement_id))
    return ts


async def entry_source_ids(db: AsyncSession, ts: Timesheet) -> tuple[str, list[uuid.UUID]]:
    if ts.kind == "adjustment":
        ids = (
            await db.execute(
                select(TimesheetAdjustmentEntry.id).where(TimesheetAdjustmentEntry.timesheet_id == ts.id)
            )
        ).scalars()
        return SOURCE_KIND_ADJUSTMENT, list(ids)
    ids = (await db.execute(select(TimesheetEntry.id).where(TimesheetEntry.timesheet_id == ts.id))).scalars()
    return SOURCE_KIND_ENTRY, list(ids)


async def lock_flags(db: AsyncSession, ts: Timesheet) -> LockFlags:
    """Derive the lock flags from the ledgers (AD-21), display only."""
    kind, ids = await entry_source_ids(db, ts)
    if not ids:
        return LockFlags(payroll_locked=False, billing_locked=False, in_draft_invoice=False)
    payable = (
        await db.execute(
            select(PayablePosting.id)
            .where(
                PayablePosting.source_kind == kind,
                PayablePosting.source_id.in_(ids),
                PayablePosting.state != "released",
            )
            .limit(1)
        )
    ).first()
    billing_states = set(
        (
            await db.execute(
                select(BillingPosting.state).where(
                    BillingPosting.source_kind == kind,
                    BillingPosting.source_id.in_(ids),
                    BillingPosting.state != "released",
                )
            )
        ).scalars()
    )
    return LockFlags(
        payroll_locked=payable is not None,
        billing_locked="posted" in billing_states,
        in_draft_invoice="reserved" in billing_states,
    )


async def reopen(ctx: RequestContext, db: AsyncSession, ts: Timesheet, reason: str) -> Timesheet:
    actor = actor_from_ctx(ctx)
    if ts.status != "approved":
        raise transition_not_allowed(ts.status, "draft")
    flags = await lock_flags(db, ts)
    if flags.payroll_locked or flags.billing_locked:
        raise ConflictError(
            "Posted hours cannot be reopened; create an adjustment timesheet",
            code="timesheet_locked",
            details=flags.model_dump(),
        )
    if flags.in_draft_invoice:
        raise ConflictError(
            "These hours are on a draft invoice; discard it first",
            code="in_draft_invoice",
            details=flags.model_dump(),
        )
    transition_timesheet(ts, "draft", actor)
    ts.approved_at = None
    ts.submitted_at = None
    ts.reason = reason
    await db.flush()
    await collapse_split(db, ts)
    emit(db, TimesheetReopened(tenant_id=ts.tenant_id, timesheet_id=ts.id, placement_id=ts.placement_id))
    return ts


# ================================================================ copy previous / prefill
async def copy_previous(ctx: RequestContext, db: AsyncSession, ts: Timesheet) -> Timesheet:
    from app.services.time.common import settings_row

    if not (await settings_row(db, ts.tenant_id)).copy_previous_enabled:
        raise invalid("copy_previous_disabled", "Copying the previous timesheet is turned off")
    if ts.status not in EDITABLE or ts.kind != "regular":
        raise not_editable(ts.status)
    prev = (
        await db.execute(
            select(Timesheet)
            .where(
                Timesheet.placement_id == ts.placement_id,
                Timesheet.kind == "regular",
                Timesheet.period_start < ts.period_start,
            )
            .order_by(Timesheet.period_start.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prev is None:
        raise invalid("no_previous_timesheet", "There is no earlier timesheet to copy")
    taken = {(e.entry_date, e.category) for e in await _entries(db, ts.id)}
    cells: list[DayEntryIn] = []
    for e in await _entries(db, prev.id):
        if e.prefilled or e.category in COMPUTED_CATEGORIES:
            continue
        day = ts.period_start + timedelta(days=(e.entry_date - prev.period_start).days)
        if day > ts.period_end or (day, e.category) in taken:
            continue
        hours = e.entered_hours if e.category == "regular" and e.entered_hours is not None else e.hours
        cells.append(DayEntryIn(date=day, category=e.category, hours=str(hours)))
    placement = await get_placement(db, ts.placement_id)
    lo = max(ts.period_start, placement.actual_start_date or ts.period_start)
    hi = min(d for d in (ts.period_end, placement.end_date) if d is not None)
    cells = [c for c in cells if lo <= c.date <= hi]
    if cells:
        cfg = await config_version(db, ts.placement_id, ts.config_version)
        if cfg.entry_mode != "hours":
            cells = [c for c in cells if c.category != "regular"] + [
                DayEntryIn(date=c.date, category="regular", hours=c.hours)
                for c in cells
                if c.category == "regular"
            ]
        await upsert_entries(ctx, db, ts, cells)
    return ts


async def refresh_prefill(ctx: RequestContext, db: AsyncSession, ts: Timesheet) -> Timesheet:  # ruff: ignore[unused-function-argument]
    if ts.status not in EDITABLE:
        raise not_editable(ts.status)
    cfg = await config_version(db, ts.placement_id, ts.config_version)
    await prefill.apply_prefill(db, ts, cfg)
    return ts


# ================================================================ read projections
def entry_out(e: TimesheetEntry) -> EntryOut:
    return EntryOut(
        id=e.id,
        entry_date=e.entry_date,
        category=e.category,
        hours=e.hours,
        entered_hours=e.entered_hours,
        start_time=fmt_time(e.start_time),
        end_time=fmt_time(e.end_time),
        break_minutes=e.break_minutes,
        prefilled=e.prefilled,
        is_proxy=e.is_proxy,
        holiday_id=e.holiday_id,
        leave_type_id=e.leave_type_id,
        note=e.note,
    )


async def adjustment_entries(db: AsyncSession, timesheet_id: uuid.UUID) -> list[TimesheetAdjustmentEntry]:
    return list(
        (
            await db.execute(
                select(TimesheetAdjustmentEntry)
                .where(TimesheetAdjustmentEntry.timesheet_id == timesheet_id)
                .order_by(TimesheetAdjustmentEntry.entry_date, TimesheetAdjustmentEntry.category)
            )
        ).scalars()
    )


async def _names(
    db: AsyncSession, rows: Sequence[Timesheet]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    from app.models.candidates import Candidate
    from app.models.clients import Client

    cands = {r.candidate_id for r in rows}
    clients = {r.client_id for r in rows}
    places = {r.placement_id for r in rows}
    cn: dict[uuid.UUID, str] = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cands)))).tuples().all()
        )
        if cands
        else {}
    )
    cl: dict[uuid.UUID, str] = (
        dict((await db.execute(select(Client.id, Client.name).where(Client.id.in_(clients)))).tuples().all())
        if clients
        else {}
    )
    pl: dict[uuid.UUID, str] = (
        dict(
            (await db.execute(select(Placement.id, Placement.human_id).where(Placement.id.in_(places)))).tuples().all()
        )
        if places
        else {}
    )
    return cn, cl, pl


async def summaries(db: AsyncSession, rows: Sequence[Timesheet]) -> list[TimesheetSummaryOut]:
    if not rows:
        return []
    cn, cl, pl = await _names(db, rows)
    ids = [r.id for r in rows]
    hours: dict[uuid.UUID, Decimal] = dict(
        (
            await db.execute(
                select(TimesheetEntry.timesheet_id, func.sum(TimesheetEntry.hours))
                .where(TimesheetEntry.timesheet_id.in_(ids))
                .group_by(TimesheetEntry.timesheet_id)
            )
        ).tuples().all()
    )
    hours.update(
        dict(
            (
                await db.execute(
                    select(
                        TimesheetAdjustmentEntry.timesheet_id, func.sum(TimesheetAdjustmentEntry.delta_hours)
                    )
                    .where(TimesheetAdjustmentEntry.timesheet_id.in_(ids))
                    .group_by(TimesheetAdjustmentEntry.timesheet_id)
                )
            ).tuples().all()
        )
    )
    return [
        TimesheetSummaryOut(
            id=r.id,
            human_id=r.human_id,
            placement_id=r.placement_id,
            placement_human_id=pl.get(r.placement_id),
            candidate_id=r.candidate_id,
            worker_name=cn.get(r.candidate_id),
            client_id=r.client_id,
            client_name=cl.get(r.client_id),
            kind=r.kind,
            adjustment_seq=r.adjustment_seq,
            original_timesheet_id=r.original_timesheet_id,
            period_start=r.period_start,
            period_end=r.period_end,
            status=r.status,
            total_hours=Decimal(hours.get(r.id) or 0).quantize(Decimal("0.01")),
            submitted_at=r.submitted_at,
            approved_at=r.approved_at,
        )
        for r in rows
    ]


async def list_timesheets(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    placement_id: uuid.UUID | None = None,
    status: str | None = None,
    period_start: date | None = None,
) -> list[TimesheetSummaryOut]:
    stmt = select(Timesheet).order_by(Timesheet.period_start.desc(), Timesheet.adjustment_seq)
    if placement_id is not None:
        stmt = stmt.where(Timesheet.placement_id == placement_id)
    if status:
        stmt = stmt.where(Timesheet.status == status)
    if period_start is not None:
        stmt = stmt.where(Timesheet.period_start == period_start)
    rows = list((await db.execute(stmt.limit(500))).scalars())
    return await summaries(db, rows)


async def approval_history(db: AsyncSession, ts: Timesheet) -> tuple[list[ApprovalStepOut], str | None]:
    if ts.approval_request_id is None:
        return [], None
    request = (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == ts.approval_request_id))
    ).scalar_one_or_none()
    if request is None:
        return [], None
    decisions = {
        d.position: d
        for d in (
            await db.execute(
                select(ApprovalDecision)
                .where(ApprovalDecision.request_id == request.id)
                .order_by(ApprovalDecision.decided_at)
            )
        ).scalars()
    }
    out: list[ApprovalStepOut] = []
    current: str | None = None
    for step in sorted(request.chain_snapshot or [], key=lambda s: int(s["position"])):
        pos = int(step["position"])
        d = decisions.get(pos)
        out.append(
            ApprovalStepOut(
                position=pos,
                name=str(step.get("name") or ""),
                outcome=d.outcome if d else None,
                actor_kind=d.actor_kind if d else None,
                comment=d.comment if d else None,
                decided_at=d.decided_at if d else None,
            )
        )
        if request.status == "pending" and request.current_position == pos:
            current = str(step.get("name") or "")
    return out, current


async def detail(ctx: RequestContext, db: AsyncSession, timesheet_id: uuid.UUID) -> TimesheetDetailOut:  # ruff: ignore[unused-function-argument]
    ts = await get_timesheet(db, timesheet_id)
    base = (await summaries(db, [ts]))[0]
    cfg = await config_version(db, ts.placement_id, ts.config_version)
    entries = await _entries(db, ts.id)
    adjustments = await adjustment_entries(db, ts.id)
    history, current = await approval_history(db, ts)
    totals = (
        _totals((a.category, a.delta_hours) for a in adjustments)
        if ts.kind == "adjustment"
        else _totals((e.category, e.hours) for e in entries)
    )
    return TimesheetDetailOut(
        **base.model_dump(),
        config_version=ts.config_version,
        entry_mode=cfg.entry_mode,
        hours_per_day=cfg.hours_per_day,
        daily_cap=cfg.daily_cap,
        enabled_categories=list(cfg.enabled_categories or []),
        entries=[entry_out(e) for e in entries],
        adjustment_entries=[
            AdjustmentEntryOut(
                id=a.id, entry_date=a.entry_date, category=a.category, delta_hours=a.delta_hours, note=a.note
            )
            for a in adjustments
        ],
        totals=totals,
        locks=await lock_flags(db, ts),
        approval_request_id=ts.approval_request_id,
        current_step=current,
        approval_history=history,
        rejection_comment=ts.rejection_comment,
        proxy_reason=ts.proxy_reason,
        reason=ts.reason,
        client_approval_evidence_file_id=ts.client_approval_evidence_file_id,
    )
