# ruff: noqa: E501, D401
"""Timesheet flag evaluation and reads (Epic 63 A1, FR-312).

* ``evaluate(db, timesheet_id)`` -- on every ``TimesheetSubmitted`` (task ``timesheet_flags.evaluate``, queue
  ``interactive``), with AI on or off: loads the facts (entries, config, approved leave, counted holidays, the last 8
  approved periods, client/project peers, PO hours left), runs the pure rules and stores one row per hit for the
  timesheet's ``lock_version``. Idempotent per version; flags of earlier versions become ``superseded``. Emits
  ``TimesheetFlagsRaised`` when there are flags.
* ``list_for_timesheet`` / ``list_flags`` / ``review`` (audited) / ``set_explanations`` (the agent tool, pure number
  check against the facts).

Flags never block approval and never change hours; nothing here touches ``timesheets`` / entries.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Scope
from app.events.timesheet_flags import TimesheetFlagsRaised
from app.models.placements import Placement
from app.models.time import LeaveRequest, Timesheet, TimesheetEntry
from app.models.timesheet_flags import TimesheetFlag
from app.schemas.timesheet_flags import FlagExplanationIn, FlagListRow, FlagOut, FlagReviewIn
from app.services.timesheet_flags import rules

HISTORY_PERIODS = 8
OPEN_STATUSES = ("open", "reviewed")
PEER_STATUSES = ("submitted", "in_approval", "approved")


def to_out(row: TimesheetFlag) -> FlagOut:
    return FlagOut(
        id=row.id, timesheet_id=row.timesheet_id, placement_id=row.placement_id, rule=row.rule,
        label=rules.LABELS.get(row.rule, row.rule), severity=row.severity, entry_dates=list(row.entry_dates or []),
        facts=dict(row.facts or {}), group_key=row.group_key, explanation=row.explanation,
        explanation_source=row.explanation_source, rules_description=rules.describe(row.rule, row.facts or {}),
        status=row.status, reviewed_by=row.reviewed_by, reviewed_at=row.reviewed_at, review_note=row.review_note,
        timesheet_version=row.timesheet_version, created_at=row.created_at,
    )  # fmt: skip


# ---------------------------------------------------------------- facts loader
def _leave_fractions(rows: list[LeaveRequest], start: date, end: date) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for r in rows:
        d = max(r.start_date, start)
        while d <= min(r.end_date, end):
            frac = Decimal(1)
            if (d == r.start_date and r.start_half == "pm") or (d == r.end_date and r.end_half == "am"):
                frac = rules.HALF
            out[d] = max(out.get(d, Decimal(0)), frac)
            d += timedelta(days=1)
    return out


async def _entries(db: AsyncSession, timesheet_ids: list[uuid.UUID]) -> list[TimesheetEntry]:
    if not timesheet_ids:
        return []
    return list(
        (await db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_(timesheet_ids)))).scalars()
    )


async def load_input(db: AsyncSession, ts: Timesheet) -> rules.FlagInput:
    from app.services import commercials_service, time_service

    entries = await _entries(db, [ts.id])
    try:
        cfg = await time_service.config_version(db, ts.placement_id, ts.config_version)
    except Exception:
        cfg = None
    leave_rows = list(
        (
            await db.execute(
                select(LeaveRequest).where(
                    LeaveRequest.placement_id == ts.placement_id, LeaveRequest.status == "approved",
                    LeaveRequest.start_date <= ts.period_end, LeaveRequest.end_date >= ts.period_start,
                )
            )
        ).scalars()
    )  # fmt: skip
    try:
        holidays = {h.date: h.type for h in await time_service.holidays_for(db, ts.placement_id, ts.period_start, ts.period_end)}
    except Exception:
        holidays = {}
    # history: OT + DT of the last approved regular periods of the placement
    hist_ts = list(
        (
            await db.execute(
                select(Timesheet.id).where(
                    Timesheet.placement_id == ts.placement_id, Timesheet.kind == "regular", Timesheet.status == "approved",
                    Timesheet.period_end < ts.period_start,
                ).order_by(Timesheet.period_start.desc()).limit(HISTORY_PERIODS)
            )
        ).scalars()
    )  # fmt: skip
    hist_ot: dict[uuid.UUID, Decimal] = dict.fromkeys(hist_ts, Decimal(0))
    for e in await _entries(db, hist_ts):
        if e.category in rules.OT_CATEGORIES:
            hist_ot[e.timesheet_id] += Decimal(e.hours)
    # peers: same client (and project when set), same period, other Workers
    placement = await db.get(Placement, ts.placement_id)
    peer_stmt = (
        select(Timesheet.id)
        .join(Placement, Placement.id == Timesheet.placement_id)
        .where(
            Timesheet.client_id == ts.client_id, Timesheet.kind == "regular", Timesheet.status.in_(PEER_STATUSES),
            Timesheet.period_start == ts.period_start, Timesheet.candidate_id != ts.candidate_id,
        )
    )  # fmt: skip
    if placement is not None and placement.project_id is not None:
        peer_stmt = peer_stmt.where(Placement.project_id == placement.project_id)
    peer_ids = list((await db.execute(peer_stmt.limit(200))).scalars())
    peer_totals: dict[uuid.UUID, Decimal] = dict.fromkeys(peer_ids, Decimal(0))
    for e in await _entries(db, peer_ids):
        if e.category in rules.WORK_CATEGORIES:
            peer_totals[e.timesheet_id] += Decimal(e.hours)
    po_remaining = None
    if placement is not None and placement.po_id is not None:
        try:
            bal = await commercials_service.po_balance(RequestContext(tenant_id=ts.tenant_id), db, placement.po_id)
            po_remaining = Decimal(bal.remaining_hours) if bal.remaining_hours is not None else None
        except Exception:
            po_remaining = None
    return rules.FlagInput(
        period_start=ts.period_start, period_end=ts.period_end,
        entries=tuple(rules.Entry(e.entry_date, e.category, Decimal(e.hours), e.note) for e in entries),
        work_days=frozenset(int(d) for d in (cfg.work_days if cfg is not None and cfg.work_days else (0, 1, 2, 3, 4))),
        hours_per_day=Decimal(cfg.hours_per_day) if cfg is not None and cfg.hours_per_day else Decimal(8),
        daily_cap=Decimal(cfg.daily_cap) if cfg is not None and cfg.daily_cap else Decimal(24),
        leave=_leave_fractions(leave_rows, ts.period_start, ts.period_end), holidays=holidays,
        history_ot=tuple(hist_ot[i] for i in hist_ts), peer_totals=tuple(peer_totals.values()),
        po_remaining_hours=po_remaining,
    )  # fmt: skip


# ---------------------------------------------------------------- evaluate
async def evaluate(db: AsyncSession, timesheet_id: uuid.UUID) -> list[TimesheetFlag]:
    ts = await db.get(Timesheet, timesheet_id)
    if ts is None or ts.kind != "regular":
        return []
    version = int(ts.lock_version)
    existing = list(
        (
            await db.execute(
                select(TimesheetFlag).where(
                    TimesheetFlag.timesheet_id == ts.id, TimesheetFlag.timesheet_version == version
                )
            )
        ).scalars()
    )
    await db.execute(
        update(TimesheetFlag)
        .where(
            TimesheetFlag.timesheet_id == ts.id, TimesheetFlag.timesheet_version != version,
            TimesheetFlag.status.in_(OPEN_STATUSES),
        )
        .values(status="superseded")
    )  # fmt: skip
    if existing:
        return existing
    hits = rules.evaluate(await load_input(db, ts))
    rows = [
        TimesheetFlag(
            id=uuid.uuid4(), tenant_id=ts.tenant_id, timesheet_id=ts.id, placement_id=ts.placement_id,
            candidate_id=ts.candidate_id, rule=h.rule, severity=h.severity, entry_dates=list(h.entry_dates),
            facts=h.facts, explanation_source="rules", status="open", timesheet_version=version,
        )
        for h in hits
    ]  # fmt: skip
    for r in rows:
        db.add(r)
    await db.flush()
    if rows:
        emit(
            db,
            TimesheetFlagsRaised(
                tenant_id=ts.tenant_id, timesheet_id=ts.id, placement_id=ts.placement_id, flag_count=len(rows),
                max_severity="warning" if any(r.severity == "warning" for r in rows) else "info",
                timesheet_version=version,
            ),
        )  # fmt: skip
    return rows


async def supersede(db: AsyncSession, timesheet_id: uuid.UUID) -> int:
    """A rejected / reopened timesheet: its open flags are superseded (the resubmission is evaluated anew)."""
    res = await db.execute(
        update(TimesheetFlag)
        .where(TimesheetFlag.timesheet_id == timesheet_id, TimesheetFlag.status.in_(OPEN_STATUSES))
        .values(status="superseded")
    )
    return int(getattr(res, "rowcount", 0) or 0)


# ---------------------------------------------------------------- reads
async def _timesheet(db: AsyncSession, timesheet_id: uuid.UUID) -> Timesheet:
    ts = await db.get(Timesheet, timesheet_id)
    if ts is None:
        raise NotFound("Timesheet not found")
    return ts


async def list_for_timesheet(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    timesheet_id: uuid.UUID,
    *,
    include_superseded: bool = False,
) -> list[FlagOut]:
    await _timesheet(db, timesheet_id)
    stmt = select(TimesheetFlag).where(TimesheetFlag.timesheet_id == timesheet_id)
    if not include_superseded:
        stmt = stmt.where(TimesheetFlag.status != "superseded")
    rows = (await db.execute(stmt.order_by(TimesheetFlag.severity.desc(), TimesheetFlag.rule))).scalars()
    return [to_out(r) for r in rows]


async def list_flags(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    status: str | None = None,
    rule: str | None = None,
    client_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[FlagListRow], int]:
    stmt = select(TimesheetFlag, Timesheet.human_id, Timesheet.client_id).join(
        Timesheet, Timesheet.id == TimesheetFlag.timesheet_id
    )
    if status:
        stmt = stmt.where(TimesheetFlag.status == status)
    else:
        stmt = stmt.where(TimesheetFlag.status != "superseded")
    if rule:
        stmt = stmt.where(TimesheetFlag.rule == rule)
    if client_id:
        stmt = stmt.where(Timesheet.client_id == client_id)
    if date_from:
        stmt = stmt.where(Timesheet.period_end >= date_from)
    if date_to:
        stmt = stmt.where(Timesheet.period_start <= date_to)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (await db.execute(stmt.order_by(TimesheetFlag.created_at.desc()).limit(limit).offset(offset))).all()
    return [
        FlagListRow(**to_out(f).model_dump(), timesheet_human_id=hid, client_id=cid) for f, hid, cid in rows
    ], total


async def counts(db: AsyncSession, timesheet_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, str]]:
    if not timesheet_ids:
        return {}
    rows = (
        await db.execute(
            select(TimesheetFlag.timesheet_id, TimesheetFlag.severity, func.count())
            .where(TimesheetFlag.timesheet_id.in_(timesheet_ids), TimesheetFlag.status == "open")
            .group_by(TimesheetFlag.timesheet_id, TimesheetFlag.severity)
        )
    ).all()
    out: dict[uuid.UUID, tuple[int, str]] = {}
    for tid, sev, n in rows:
        prev = out.get(tid, (0, "info"))
        out[tid] = (prev[0] + int(n), "warning" if "warning" in (sev, prev[1]) else "info")
    return out


# ---------------------------------------------------------------- writes
def _user(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if principal is not None and getattr(principal, "type", "") == "user" else None


async def review(ctx: RequestContext, db: AsyncSession, flag_id: uuid.UUID, data: FlagReviewIn) -> FlagOut:
    principal = ctx.principal
    if principal is None or not (principal.has("timesheet:approve") or principal.has("timesheet:monitor")):
        raise Forbidden("You do not have permission to review timesheet flags")
    row = (
        await db.execute(select(TimesheetFlag).where(TimesheetFlag.id == flag_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Flag not found")
    if row.status == "superseded":
        raise ValidationFailed("This flag belongs to an older version of the timesheet", code="flag_superseded")
    if data.status == "dismissed" and not (data.note or "").strip():
        raise ValidationFailed("Add a note when you dismiss a flag", code="note_required")
    row.status = data.status
    row.review_note = (data.note or "").strip()[:500] or None
    row.reviewed_by = _user(ctx)
    row.reviewed_at = datetime.now(UTC)
    await db.flush()
    audit.record(f"timesheet_flag.{data.status}", "timesheet_flag", row.id, session=db, rule=row.rule)
    return to_out(row)


async def set_explanations(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    timesheet_id: uuid.UUID,
    items: list[FlagExplanationIn],
    *,
    agent_run_id: uuid.UUID | None = None,
) -> int:
    """Store the agent's explanations (``explanation_source='ai'``). Refuses (``invalid``) an explanation naming a
    number that is not in the flag facts or dates."""
    ts = await _timesheet(db, timesheet_id)
    ids = [i.flag_id for i in items]
    rows = {
        r.id: r
        for r in (
            await db.execute(
                select(TimesheetFlag).where(
                    TimesheetFlag.id.in_(ids), TimesheetFlag.timesheet_id == ts.id,
                    TimesheetFlag.status != "superseded",
                )
            )
        ).scalars()
    }  # fmt: skip
    missing = [str(i) for i in ids if i not in rows]
    if missing:
        raise NotFound("Flag not found")
    problems: list[dict[str, Any]] = []
    for item in items:
        row = rows[item.flag_id]
        bad = rules.unknown_numbers(item.explanation, row.facts or {}, row.entry_dates or [])
        if bad:
            problems.append({"flag_id": str(item.flag_id), "numbers": bad})
    if problems:
        raise ValidationFailed(
            "An explanation names numbers that are not in the flag facts",
            code="explanation_unknown_number",
            details={"problems": problems},
        )
    for item in items:
        row = rows[item.flag_id]
        row.explanation = item.explanation.strip()[:600]
        row.group_key = item.group_key
        row.explanation_source = "ai"
        row.agent_run_id = agent_run_id or row.agent_run_id
    await db.flush()
    return len(items)


def has_scope(principal: Any, permission: str) -> Scope | None:
    perms = getattr(principal, "permissions", None) or {}
    value = perms.get(permission)
    return Scope(value) if value is not None else None


__all__ = [
    "counts",
    "evaluate",
    "has_scope",
    "list_flags",
    "list_for_timesheet",
    "load_input",
    "review",
    "set_explanations",
    "supersede",
    "to_out",
]
