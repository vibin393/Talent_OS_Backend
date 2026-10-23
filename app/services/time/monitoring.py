"""Missing / late monitoring and the read contracts used by other epics (FR-133).

* ``monitor``: per Timesheet status, current step + approver, age in business days, ``missing`` (draft after
  period end + grace) / ``late`` (draft after period end, or submitted after it + grace) and
  ``estimated_unbilled_value`` = sum over billable approved-or-pending entries not yet on a billing posting of
  ``quantize(hours x bill_rate x bill_multiplier)`` **per entry** (HALF_UP), per currency, masked without
  ``compensation:view``.
* ``remind`` (one per item per 4 h), the daily ``flag_missing`` / ``scheduled_reminders`` beats, the
  ``timesheets_pending`` close check, ``unapproved_timesheets`` (Epic 28), ``timesheet_state_for`` (Epic
  26) and the
  offboarding verifier ``final_timesheet_approved`` (Epic 31).
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.money import line_taxable
from app.core.permissions import COMPENSATION_VIEW
from app.events.time import TimesheetMissing
from app.models.approvals import ApprovalRequest
from app.models.placements import Placement, RateSegment
from app.models.postings import BillingPosting, PayablePosting
from app.models.time import Timesheet, TimesheetAdjustmentEntry, TimesheetEntry, TimesheetReminderSent
from app.schemas.time import (
    CurrencyTotal,
    MonitorOut,
    MonitorRow,
    RemindOut,
    RemindSkip,
    TimesheetState,
    UnapprovedTimesheet,
)
from app.services.time import calendar
from app.services.time.common import dec, now, settings_row, tenant_of
from app.services.time.configs import config_version
from app.services.time.events import TimesheetReminderDue

REMINDER_GAP = timedelta(hours=4)
PENDING = ("submitted", "in_approval")


def _can_see_money(ctx: RequestContext) -> bool:
    principal = ctx.principal
    perms = getattr(principal, "permissions", None) or {}
    return COMPENSATION_VIEW in perms


async def _segments(db: AsyncSession, placement_ids: set[uuid.UUID]) -> dict[uuid.UUID, list[RateSegment]]:
    out: dict[uuid.UUID, list[RateSegment]] = defaultdict(list)
    if not placement_ids:
        return out
    rows = await db.execute(
        select(RateSegment)
        .where(RateSegment.placement_id.in_(placement_ids), RateSegment.status == "active")
        .order_by(RateSegment.effective_from)
    )
    for seg in rows.scalars():
        out[seg.placement_id].append(seg)
    return out


def _segment_on(segments: Sequence[RateSegment], d: date) -> RateSegment | None:
    found = None
    for seg in segments:
        if seg.effective_from <= d and (seg.effective_to is None or seg.effective_to >= d):
            found = seg
    return found


def entry_unbilled_value(
    hours: Decimal,
    bill_rate: Decimal,
    multiplier: Decimal,
    currency: str,
    *,
    rate_unit: str = "hour",
    hours_per_day: Decimal | None = None,
) -> Decimal | None:
    """One entry's estimated value, rounded once (HALF_UP). ``day`` rates convert hours via
    ``hours_per_day``."""
    if rate_unit == "hour":
        return line_taxable(hours, bill_rate, multiplier, currency)
    if rate_unit == "day" and hours_per_day:
        from app.core.money import MONEY_CONTEXT

        return line_taxable(MONEY_CONTEXT.divide(hours, hours_per_day), bill_rate, multiplier, currency)
    return None


async def _unbilled(
    db: AsyncSession, rows: Sequence[Timesheet], currencies: dict[uuid.UUID, str]
) -> dict[uuid.UUID, Decimal | None]:
    ids = [r.id for r in rows if r.status in (*PENDING, "approved") and r.kind == "regular"]
    out: dict[uuid.UUID, Decimal | None] = {}
    if not ids:
        return out
    entries = list(
        (
            await db.execute(
                select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_(ids), TimesheetEntry.hours > 0)
            )
        ).scalars()
    )
    billed = set(
        (
            await db.execute(
                select(BillingPosting.source_id).where(
                    BillingPosting.source_kind == "timesheet_entry",
                    BillingPosting.source_id.in_([e.id for e in entries] or [uuid.uuid4()]),
                    BillingPosting.state != "released",
                )
            )
        ).scalars()
    )
    by_id = {r.id: r for r in rows}
    segments = await _segments(db, {by_id[i].placement_id for i in ids})
    cfg_cache: dict[tuple[uuid.UUID, int], Any] = {}
    totals: dict[uuid.UUID, Decimal] = dict.fromkeys(ids, Decimal("0"))
    unknown: set[uuid.UUID] = set()
    for e in entries:
        ts = by_id[e.timesheet_id]
        key = (ts.placement_id, ts.config_version)
        if key not in cfg_cache:
            cfg_cache[key] = await config_version(db, ts.placement_id, ts.config_version)
        cfg = cfg_cache[key]
        mult = dec((cfg.bill_multipliers or {}).get(e.category, "0"))
        if e.id in billed or mult <= 0:
            continue
        seg = _segment_on(segments.get(ts.placement_id, []), e.entry_date)
        value = (
            None
            if seg is None or seg.bill_rate is None
            else entry_unbilled_value(
                e.hours,
                seg.bill_rate,
                mult,
                currencies[ts.id],
                rate_unit=seg.rate_unit,
                hours_per_day=cfg.hours_per_day,
            )
        )
        if value is None:
            unknown.add(ts.id)
        else:
            totals[ts.id] += value
    for tid in ids:
        out[tid] = None if tid in unknown else totals[tid]
    return out


async def monitor(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    period_start: date | None = None,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
) -> MonitorOut:
    from app.models.candidates import Candidate
    from app.models.clients import Client

    stmt = select(Timesheet).order_by(Timesheet.period_start.desc(), Timesheet.human_id)
    if period_start is not None:
        stmt = stmt.where(Timesheet.period_start <= period_start, Timesheet.period_end >= period_start)
    if legal_entity_id is not None:
        stmt = stmt.where(Timesheet.legal_entity_id == legal_entity_id)
    if client_id is not None:
        stmt = stmt.where(Timesheet.client_id == client_id)
    if status:
        stmt = stmt.where(Timesheet.status == status)
    rows = list((await db.execute(stmt.limit(2000))).scalars())
    settings = await settings_row(db, tenant_of(ctx, db))
    grace = timedelta(days=settings.missing_grace_days)
    today = date.today()
    cands = {r.candidate_id for r in rows}
    clients = {r.client_id for r in rows}
    pls = {r.placement_id for r in rows}
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
    pinfo = (
        {
            r.id: (r.human_id, r.currency)
            for r in (
                await db.execute(
                    select(Placement.id, Placement.human_id, Placement.currency).where(Placement.id.in_(pls))
                )
            ).all()
        }
        if pls
        else {}
    )
    req_ids = [
        r.approval_request_id for r in rows if r.approval_request_id is not None and r.status in PENDING
    ]
    requests = (
        {
            r.id: r
            for r in (
                await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(req_ids)))
            ).scalars()
        }
        if req_ids
        else {}
    )
    hours: dict[uuid.UUID, Decimal] = {}
    ids = [r.id for r in rows]
    if ids:
        for tid, h in (
            await db.execute(
                select(TimesheetEntry.timesheet_id, func.sum(TimesheetEntry.hours))
                .where(TimesheetEntry.timesheet_id.in_(ids))
                .group_by(TimesheetEntry.timesheet_id)
            )
        ).all():
            hours[tid] = Decimal(h)
        for tid, h in (
            await db.execute(
                select(TimesheetAdjustmentEntry.timesheet_id, func.sum(TimesheetAdjustmentEntry.delta_hours))
                .where(TimesheetAdjustmentEntry.timesheet_id.in_(ids))
                .group_by(TimesheetAdjustmentEntry.timesheet_id)
            )
        ).all():
            hours[tid] = Decimal(h)
    currencies = {r.id: pinfo.get(r.placement_id, ("", "INR"))[1] for r in rows}
    masked = not _can_see_money(ctx)
    values = {} if masked else await _unbilled(db, rows, currencies)
    out_rows: list[MonitorRow] = []
    totals: dict[str, Decimal | None] = {}
    for r in rows:
        request = requests.get(r.approval_request_id) if r.approval_request_id else None  # nosec B113 - 'requests' is a local dict .get(), not the requests HTTP library
        step = approver = None
        if request is not None and request.current_position is not None:
            snap = next(
                (s for s in request.chain_snapshot if int(s["position"]) == request.current_position), None
            )
            step = str(snap.get("name")) if snap else None
            approver = request.current_approver_kind
        anchor = r.submitted_at.date() if r.submitted_at else r.period_end
        missing = r.status == "draft" and r.kind == "regular" and today > r.period_end + grace
        late = (r.status == "draft" and r.kind == "regular" and today > r.period_end) or (
            r.submitted_at is not None and r.submitted_at.date() > r.period_end + grace
        )
        value = None if masked else values.get(r.id)
        currency = currencies[r.id]
        if not masked and r.id in values:
            prev = totals.get(currency, Decimal("0"))
            totals[currency] = None if prev is None or value is None else prev + value
        out_rows.append(
            MonitorRow(
                timesheet_id=r.id,
                human_id=r.human_id,
                placement_id=r.placement_id,
                placement_human_id=pinfo.get(r.placement_id, (None, ""))[0],
                candidate_id=r.candidate_id,
                worker_name=cn.get(r.candidate_id),
                client_id=r.client_id,
                client_name=cl.get(r.client_id),
                legal_entity_id=r.legal_entity_id,
                kind=r.kind,
                period_start=r.period_start,
                period_end=r.period_end,
                status=r.status,
                current_step=step,
                current_approver=approver,
                age_business_days=calendar.business_days_between(anchor, today),
                missing=missing,
                late=late,
                total_hours=hours.get(r.id, Decimal("0")).quantize(Decimal("0.01")),
                currency=currency,
                estimated_unbilled_value=value,
            )
        )
    return MonitorOut(
        rows=out_rows,
        totals=[CurrencyTotal(currency=c, estimated_unbilled_value=v) for c, v in sorted(totals.items())],
        masked=masked,
    )


# ---------------------------------------------------------------- reminders
async def _recipient(db: AsyncSession, ts: Timesheet) -> tuple[str, uuid.UUID] | None:
    if ts.status in ("draft", "rejected"):
        return "candidate", ts.candidate_id
    if ts.status in PENDING and ts.approval_request_id is not None:
        request = (
            await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == ts.approval_request_id))
        ).scalar_one_or_none()
        if request is not None and request.current_approver_user_id is not None:
            return "user", request.current_approver_user_id
        if request is not None and request.current_approver_client_user_id is not None:
            return "client_user", request.current_approver_client_user_id
    return None


async def remind(
    ctx: RequestContext, db: AsyncSession, timesheet_ids: Sequence[uuid.UUID], *, kind: str = "manual"
) -> RemindOut:
    sent: list[uuid.UUID] = []
    skipped: list[RemindSkip] = []
    cutoff = now() - REMINDER_GAP
    for tid in timesheet_ids:
        ts = (await db.execute(select(Timesheet).where(Timesheet.id == tid))).scalar_one_or_none()
        if ts is None:
            skipped.append(RemindSkip(timesheet_id=tid, code="not_found"))
            continue
        recent = (
            await db.execute(
                select(TimesheetReminderSent.id)
                .where(TimesheetReminderSent.timesheet_id == ts.id, TimesheetReminderSent.sent_at > cutoff)
                .limit(1)
            )
        ).first()
        if recent is not None:
            skipped.append(RemindSkip(timesheet_id=tid, code="reminder_too_soon"))
            continue
        target = await _recipient(db, ts)
        if target is None:
            skipped.append(RemindSkip(timesheet_id=tid, code="nobody_to_remind"))
            continue
        db.add(
            TimesheetReminderSent(
                id=uuid.uuid4(),
                tenant_id=ts.tenant_id,
                timesheet_id=ts.id,
                kind=kind,
                recipient_kind=target[0],
                recipient_ref=target[1],
                sent_by=getattr(ctx.principal, "id", None),
            )
        )
        emit(
            db,
            TimesheetReminderDue(
                tenant_id=ts.tenant_id,
                timesheet_id=ts.id,
                placement_id=ts.placement_id,
                recipient_kind=target[0],
                recipient_id=target[1],
            ),
        )
        sent.append(tid)
    await db.flush()
    return RemindOut(sent=sent, skipped=skipped)


async def scheduled_reminders(ctx: RequestContext, db: AsyncSession, today: date | None = None) -> int:
    """Daily: drafts whose period ended ``offset`` days ago (tenant ``reminder_offsets``)."""
    today = today or date.today()
    settings = await settings_row(db, tenant_of(ctx, db))
    ends = [today - timedelta(days=int(o)) for o in settings.reminder_offsets or []]
    if not ends:
        return 0
    ids = list(
        (
            await db.execute(
                select(Timesheet.id).where(
                    Timesheet.status == "draft", Timesheet.kind == "regular", Timesheet.period_end.in_(ends)
                )
            )
        ).scalars()
    )
    return len((await remind(ctx, db, ids, kind="scheduled")).sent) if ids else 0


async def flag_missing(ctx: RequestContext, db: AsyncSession, today: date | None = None) -> int:
    today = today or date.today()
    settings = await settings_row(db, tenant_of(ctx, db))
    cutoff = today - timedelta(days=settings.missing_grace_days)
    rows = list(
        (
            await db.execute(
                select(Timesheet).where(
                    Timesheet.status == "draft",
                    Timesheet.kind == "regular",
                    Timesheet.period_end < cutoff,
                    Timesheet.missing_flagged_at.is_(None),
                )
            )
        ).scalars()
    )
    for ts in rows:
        ts.missing_flagged_at = now()
        emit(
            db,
            TimesheetMissing(
                tenant_id=ts.tenant_id,
                timesheet_id=ts.id,
                placement_id=ts.placement_id,
                period_end=ts.period_end,
            ),
        )
    await db.flush()
    return len(rows)


# ---------------------------------------------------------------- read contracts
async def unapproved_timesheets(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> list[UnapprovedTimesheet]:
    rows = await db.execute(
        select(Timesheet)
        .where(
            Timesheet.legal_entity_id == legal_entity_id,
            Timesheet.status != "approved",
            Timesheet.period_start <= period_end,
            Timesheet.period_end >= period_start,
        )
        .order_by(Timesheet.period_start, Timesheet.human_id)
    )
    return [
        UnapprovedTimesheet(
            timesheet_id=t.id,
            human_id=t.human_id,
            placement_id=t.placement_id,
            candidate_id=t.candidate_id,
            legal_entity_id=t.legal_entity_id,
            kind=t.kind,
            period_start=t.period_start,
            period_end=t.period_end,
            status=t.status,
        )
        for t in rows.scalars()
    ]


async def _locked(db: AsyncSession, timesheet_id: uuid.UUID) -> bool:
    ids = list(
        (
            await db.execute(select(TimesheetEntry.id).where(TimesheetEntry.timesheet_id == timesheet_id))
        ).scalars()
    )
    if not ids:
        return False
    for model in (BillingPosting, PayablePosting):
        hit = (
            await db.execute(
                select(model.id)
                .where(
                    model.source_kind == "timesheet_entry",
                    model.source_id.in_(ids),
                    model.state != "released",
                )
                .limit(1)
            )
        ).first()
        if hit is not None:
            return True
    return False


async def timesheet_state_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    placement_id: uuid.UUID,
    on_date: date,
) -> TimesheetState | None:
    """Return the regular Timesheet covering ``on_date`` (``None`` when none exists yet: treat as
    editable)."""
    ts = (
        await db.execute(
            select(Timesheet).where(
                Timesheet.placement_id == placement_id,
                Timesheet.kind == "regular",
                Timesheet.period_start <= on_date,
                Timesheet.period_end >= on_date,
            )
        )
    ).scalar_one_or_none()
    if ts is None:
        return None
    return TimesheetState(timesheet_id=ts.id, status=ts.status, locked=await _locked(db, ts.id))


async def close_check(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date
) -> Any:
    from app.services.legal_entities.periods import CloseCheckResult

    pending = await unapproved_timesheets(ctx, db, legal_entity_id, start, end)
    return CloseCheckResult(
        name="timesheets_pending",
        label="Timesheets not approved",
        blocking=False,
        count=len(pending),
        link_path="/timesheets?status=pending",
    )


async def final_timesheet_approved(db: AsyncSession, placement_id: uuid.UUID, last_working_day: date) -> Any:
    from app.services.placements_service import VerifierResult

    rows = list(
        (
            await db.execute(
                select(Timesheet.id, Timesheet.status, Timesheet.period_start, Timesheet.period_end).where(
                    Timesheet.placement_id == placement_id,
                    Timesheet.kind == "regular",
                    Timesheet.period_start <= last_working_day,
                )
            )
        ).all()
    )
    covering = [r for r in rows if r.period_start <= last_working_day <= r.period_end]
    unapproved = [str(r.id) for r in rows if r.status != "approved"]
    done = bool(covering) and not unapproved
    return VerifierResult(
        done=done, details={"unapproved_timesheet_ids": unapproved, "final_period_exists": bool(covering)}
    )


async def tenant_pending_digest(ctx: RequestContext, db: AsyncSession) -> int:
    """Daily digest per internal approver of timesheet steps pending on them."""
    from app.services.time.events import TimesheetApprovalsDigestDue

    rows = (
        await db.execute(
            select(ApprovalRequest.current_approver_user_id, func.count())
            .where(
                ApprovalRequest.subject_type == "timesheet",
                ApprovalRequest.status == "pending",
                ApprovalRequest.current_approver_user_id.is_not(None),
            )
            .group_by(ApprovalRequest.current_approver_user_id)
        )
    ).all()
    tenant_id = tenant_of(ctx, db)
    for user_id, count in rows:
        emit(db, TimesheetApprovalsDigestDue(tenant_id=tenant_id, user_id=user_id, pending_count=int(count)))
    return len(rows)
