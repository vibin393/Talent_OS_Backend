"""Payroll settings, Pay Calendars and Pay Periods (Epic 28 A1-A2, FR-140, AD-22).

* Pure: ``period_end_for`` / ``period_containing`` / ``next_period`` (cut-off maths), ``map_period`` (item ->
  period, ``item_date`` / ``approval_date`` modes, Legal Entity timezone).
* ``ensure_pay_periods`` generates periods 12 months ahead, idempotently; a cut-off change only affects
  periods after the last generated one (no overlap -- the EXCLUDE constraint refuses overlap anyway, 409
  ``pay_period_overlap``).
* ``reopen`` (Finance Admin, human only, reason required) -> ``open``; finalized versions never change and the
  next finalize in that period is a supplementary.
"""

import calendar as _cal
import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.payroll import PayPeriodReopened
from app.models.legal_entities import LegalEntity
from app.models.payroll import PayCalendar, PayPeriod, PayrollExportVersion, PayrollSettings
from app.schemas.payroll import (
    PayCalendarIn,
    PayCalendarOut,
    PayPeriodOut,
    PayPeriodRef,
    PayrollSettingsOut,
    PayrollSettingsUpdate,
)

MONTHS_AHEAD = 12
MONTHS_BACK = 3


# ================================================================ pure
def _last_day(year: int, month: int) -> int:
    return _cal.monthrange(year, month)[1]


def period_end_for(d: date, cutoff: int | None) -> date:
    """Return the first cut-off date on or after ``d`` (``None`` = last day of the month)."""
    if cutoff is None:
        return date(d.year, d.month, _last_day(d.year, d.month))
    if d.day <= cutoff:
        return date(d.year, d.month, cutoff)
    y, m = fiscal.add_months(d.year, d.month, 1)
    return date(y, m, cutoff)


def period_containing(d: date, cutoff: int | None) -> tuple[date, date]:
    end = period_end_for(d, cutoff)
    py, pm = fiscal.add_months(end.year, end.month, -1)
    prev_end = date(py, pm, _last_day(py, pm)) if cutoff is None else date(py, pm, cutoff)
    return prev_end + timedelta(days=1), end


def next_period(after_end: date, cutoff: int | None) -> tuple[date, date]:
    start = after_end + timedelta(days=1)
    return start, period_end_for(start, cutoff)


@dataclasses.dataclass(frozen=True, slots=True)
class PeriodView:
    id: uuid.UUID
    start: date
    end: date
    status: str
    locked_at: datetime | None


def _covering(d: date, periods: Sequence[PeriodView]) -> PeriodView | None:
    return next((p for p in periods if p.start <= d <= p.end), None)


def map_period(
    item_date: date,
    approved_at: datetime | None,
    mode: str,
    periods: Sequence[PeriodView],
    tz: str = "Asia/Kolkata",
) -> PeriodView | None:
    """Return the pay period an approved item belongs to. ``approval_date`` mode: an item approved after its
    item-date period was Locked goes to the period containing the approval's local date (LE timezone)."""
    own = _covering(item_date, periods)
    if mode != "approval_date" or own is None or approved_at is None:
        return own
    if own.status == "locked" and own.locked_at is not None and approved_at > own.locked_at:
        return _covering(fiscal.local_date(approved_at, tz), periods) or own
    return own


# ================================================================ helpers
def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def user_of(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return getattr(p, "id", None) if getattr(p, "type", None) == "user" else None


def now() -> datetime:
    return datetime.now(UTC)


async def legal_entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntity:
    le = (await db.execute(select(LegalEntity).where(LegalEntity.id == legal_entity_id))).scalar_one_or_none()
    if le is None:
        raise NotFound("Legal entity not found")
    return le


# ================================================================ settings
async def get_settings_row(db: AsyncSession) -> PayrollSettings:
    row = await db.get(PayrollSettings, tenant_of(db))
    if row is None:
        row = PayrollSettings(
            tenant_id=tenant_of(db),
            late_item_mapping="item_date",
            include_worker_types=["employee", "contractor"],
        )
        db.add(row)
        await db.flush()
    return row


def _settings_out(row: PayrollSettings) -> PayrollSettingsOut:
    return PayrollSettingsOut(
        late_item_mapping=row.late_item_mapping, include_worker_types=list(row.include_worker_types)
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> PayrollSettingsOut:  # noqa: ARG001
    return _settings_out(await get_settings_row(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: PayrollSettingsUpdate
) -> PayrollSettingsOut:
    del ctx
    row = await get_settings_row(db)
    if data.late_item_mapping is not None:
        row.late_item_mapping = data.late_item_mapping
    if data.include_worker_types is not None:
        if not data.include_worker_types:
            raise ValidationFailed("Include at least one worker type", code="worker_types_required")
        row.include_worker_types = list(dict.fromkeys(data.include_worker_types))
    await db.flush()
    audit.record("payroll.settings_updated", "payroll_settings", row.tenant_id, session=db)
    return _settings_out(row)


# ================================================================ calendars
async def _calendar(db: AsyncSession, legal_entity_id: uuid.UUID) -> PayCalendar | None:
    return (
        await db.execute(select(PayCalendar).where(PayCalendar.legal_entity_id == legal_entity_id))
    ).scalar_one_or_none()


def _cal_out(row: PayCalendar | None, le: LegalEntity) -> PayCalendarOut:
    return PayCalendarOut(
        id=row.id if row else None,
        legal_entity_id=le.id,
        frequency="monthly",
        cutoff_day=(row.cutoff_day if row and row.cutoff_day is not None else "last"),
        active=row.active if row else False,
        timezone=le.timezone,
    )


async def get_calendar(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID) -> PayCalendarOut:  # noqa: ARG001
    le = await legal_entity(db, legal_entity_id)
    return _cal_out(await _calendar(db, legal_entity_id), le)


async def put_calendar(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, data: PayCalendarIn
) -> PayCalendarOut:
    le = await legal_entity(db, legal_entity_id)
    row = await _calendar(db, legal_entity_id)
    cutoff = None if data.cutoff_day == "last" else int(data.cutoff_day)
    if row is None:
        row = PayCalendar(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            legal_entity_id=le.id,
            frequency="monthly",
            cutoff_day=cutoff,
        )
        db.add(row)
    row.cutoff_day = cutoff
    row.active = data.active
    await db.flush()
    audit.record("payroll.calendar_updated", "pay_calendar", row.id, session=db, cutoff_day=cutoff)
    if row.active:
        await ensure_pay_periods(ctx, db, legal_entity_id=le.id, from_date=data.generate_from)
    return _cal_out(row, le)


def _human_id(le: LegalEntity, end: date) -> str:
    code = "".join(ch for ch in (le.name or "LE").upper() if ch.isalnum())[:6] or "LE"
    return f"PP-{end.year:04d}-{end.month:02d}-{code}"


async def ensure_pay_periods(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID | None = None,
    from_date: date | None = None,
    today: date | None = None,
) -> int:
    """Generate periods up to 12 months ahead for every active calendar (idempotent). Returns the count."""
    stmt = select(PayCalendar).where(PayCalendar.active.is_(True))
    if legal_entity_id is not None:
        stmt = stmt.where(PayCalendar.legal_entity_id == legal_entity_id)
    created = 0
    for cal in (await db.execute(stmt)).scalars().all():
        le = await legal_entity(db, cal.legal_entity_id)
        base = today or fiscal.today_for(le.timezone)
        hy, hm = fiscal.add_months(base.year, base.month, MONTHS_AHEAD)
        horizon = date(hy, hm, 1)
        existing = list(
            (
                await db.execute(
                    select(PayPeriod.period_start, PayPeriod.period_end)
                    .where(PayPeriod.legal_entity_id == le.id)
                    .order_by(PayPeriod.period_start)
                )
            ).all()
        )
        spans: list[tuple[date, date]] = []
        if from_date is not None and (not existing or from_date < existing[0][0]):
            start, end = period_containing(from_date, cal.cutoff_day)
            limit = existing[0][0] if existing else horizon
            while start < limit and end < limit:
                spans.append((start, end))
                start, end = next_period(end, cal.cutoff_day)
        if existing:
            last_end = existing[-1][1]
        elif spans:
            last_end = spans[-1][1]
        else:
            by, bm = fiscal.add_months(base.year, base.month, -MONTHS_BACK)
            s, e = period_containing(date(by, bm, 1), cal.cutoff_day)
            spans.append((s, e))
            last_end = e
        while True:
            s, e = next_period(last_end, cal.cutoff_day)
            if s >= horizon:
                break
            spans.append((s, e))
            last_end = e
        for s, e in spans:
            try:
                async with db.begin_nested():
                    db.add(
                        PayPeriod(
                            id=uuid.uuid4(),
                            tenant_id=cal.tenant_id,
                            legal_entity_id=le.id,
                            human_id=_human_id(le, e),
                            period_start=s,
                            period_end=e,
                            status="open",
                        )
                    )
                    await db.flush()
                created += 1
            except IntegrityError:
                continue  # an overlapping period already exists (EXCLUDE) -- idempotent
    return created


# ================================================================ periods
async def load_period(db: AsyncSession, period_id: uuid.UUID, *, lock: bool = False) -> PayPeriod:
    stmt = select(PayPeriod).where(PayPeriod.id == period_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Pay period not found")
    return row


async def _version_counts(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(
            PayrollExportVersion.pay_period_id,
            PayrollExportVersion.kind,
            PayrollExportVersion.status,
            func.count(),
        )
        .where(PayrollExportVersion.pay_period_id.in_(list(ids)), PayrollExportVersion.kind != "preview")
        .group_by(PayrollExportVersion.pay_period_id, PayrollExportVersion.kind, PayrollExportVersion.status)
    )
    out: dict[uuid.UUID, dict[str, int]] = {}
    for pid, kind, status, n in rows.all():
        d = out.setdefault(pid, {"final": 0, "supplementary": 0, "processed": 0, "unprocessed": 0})
        d[kind] += n
        d["processed" if status == "processed" else "unprocessed"] += n
    return out


def period_out(p: PayPeriod, counts: dict[str, int] | None = None) -> PayPeriodOut:
    c = counts or {}
    return PayPeriodOut(
        id=p.id,
        human_id=p.human_id,
        legal_entity_id=p.legal_entity_id,
        period_start=p.period_start,
        period_end=p.period_end,
        status=p.status,
        locked_at=p.locked_at,
        reopened_count=p.reopened_count,
        last_reopen_reason=p.last_reopen_reason,
        final_count=c.get("final", 0),
        supplementary_count=c.get("supplementary", 0),
        processed=bool(c.get("processed")) and not c.get("unprocessed"),
    )


async def list_periods(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[PayPeriodOut]:
    stmt = select(PayPeriod).order_by(PayPeriod.period_start.desc())
    if legal_entity_id is not None:
        stmt = stmt.where(PayPeriod.legal_entity_id == legal_entity_id)
    if status is not None:
        stmt = stmt.where(PayPeriod.status == status)
    rows = list((await db.execute(stmt)).scalars())
    counts = await _version_counts(db, [p.id for p in rows])
    return [period_out(p, counts.get(p.id)) for p in rows]


async def get_period(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID) -> PayPeriodOut:  # noqa: ARG001
    p = await load_period(db, period_id)
    return period_out(p, (await _version_counts(db, [p.id])).get(p.id))


async def period_views(db: AsyncSession, legal_entity_id: uuid.UUID) -> list[PeriodView]:
    rows = await db.execute(
        select(
            PayPeriod.id, PayPeriod.period_start, PayPeriod.period_end, PayPeriod.status, PayPeriod.locked_at
        )
        .where(PayPeriod.legal_entity_id == legal_entity_id)
        .order_by(PayPeriod.period_start)
    )
    return [PeriodView(*r) for r in rows.all()]


async def pay_period_for(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    on_date: date,
) -> PayPeriodRef | None:
    p = (
        await db.execute(
            select(PayPeriod).where(
                PayPeriod.legal_entity_id == legal_entity_id,
                PayPeriod.period_start <= on_date,
                PayPeriod.period_end >= on_date,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        return None
    return PayPeriodRef(
        id=p.id,
        legal_entity_id=p.legal_entity_id,
        start_date=p.period_start,
        end_date=p.period_end,
        status=p.status,
    )


async def reopen(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID, reason: str) -> PayPeriodOut:
    """Locked -> Open (Finance Admin, ``period:reopen_pay``, human only, audited). Finalized versions are
    untouched; the next finalize is a supplementary."""
    require_human(actor_from_ctx(ctx))
    p = await load_period(db, period_id, lock=True)
    if p.status != "locked":
        raise ConflictError("Only a locked pay period can be reopened", code="pay_period_not_locked")
    p.status = "open"
    p.reopened_count += 1
    p.last_reopen_reason = reason
    await db.flush()
    audit.record("payroll.period_reopened", "pay_period", p.id, session=db, reason=reason)
    emit(
        db,
        PayPeriodReopened(
            tenant_id=p.tenant_id, pay_period_id=p.id, legal_entity_id=p.legal_entity_id, reason=reason
        ),
    )
    return await get_period(ctx, db, p.id)
