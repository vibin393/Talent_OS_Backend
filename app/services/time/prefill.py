"""Timesheet prefill registry (A6; consumed by Epic 26 Leave).

``register_prefill_source(name, fn)`` with ``fn(db, placement_id, period_start, period_end) ->
list[PrefillItem]``.
Prefill runs when a Timesheet is created and on the Worker's "Refresh prefill" while ``draft``; it only ever
touches ``draft`` Timesheets, so calendar / leave changes never alter a submitted one (FR-134). Prefilled
rows are
``prefilled=true``. The Leave epic registers ``holidays`` and ``approved_leave``; until then the registry
is empty.

The ``holidays`` source also feeds working-day maths (``holiday_dates``) so holidays are counted in one place.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.registry import Registry
from app.models.time import Timesheet, TimesheetConfig, TimesheetEntry
from app.schemas.time import PrefillItem
from app.services.time.common import DAY_MAX, ZERO

PrefillSource = Callable[[AsyncSession, uuid.UUID, date, date], Awaitable[list[PrefillItem]]]

HOLIDAY_SOURCE = "holidays"
LEAVE_SOURCE = "approved_leave"

_sources: Registry[str, PrefillSource] = Registry("timesheet prefill source", allow_replace=True)


def register_prefill_source(name: str, fn: PrefillSource) -> None:
    _sources.register(name, fn)


def unregister_prefill_source(name: str) -> None:
    _sources.unregister(name)


def prefill_sources() -> Mapping[str, PrefillSource]:
    return _sources.view()


async def holiday_dates(db: AsyncSession, placement_id: uuid.UUID, start: date, end: date) -> set[date]:
    fn = _sources.find(HOLIDAY_SOURCE)
    if fn is None:
        return set()
    try:
        return {i.date for i in await fn(db, placement_id, start, end)}
    except Exception:
        logger.exception("holiday prefill source failed for placement {}", placement_id)
        return set()


def _enabled(name: str, cfg: TimesheetConfig) -> bool:
    if name == HOLIDAY_SOURCE:
        return bool(cfg.prefill_holidays)
    if name == LEAVE_SOURCE:
        return bool(cfg.prefill_leave)
    return True


async def apply_prefill(db: AsyncSession, ts: Timesheet, cfg: TimesheetConfig) -> int:
    """Replace the prefilled rows of a ``draft`` / ``rejected`` Timesheet from every enabled source; manual
    rows of
    the same cell win. Returns the number of prefilled rows written."""
    if ts.status not in ("draft", "rejected") or ts.kind != "regular":
        return 0
    await db.execute(
        delete(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id, TimesheetEntry.prefilled.is_(True))
    )
    manual = {
        (r.entry_date, r.category)
        for r in (
            await db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id))
        ).scalars()
    }
    enabled = set(cfg.enabled_categories or [])
    written = 0
    seen: set[tuple[date, str]] = set()
    for name, fn in sorted(_sources.view().items()):
        if not _enabled(name, cfg):
            continue
        try:
            items = await fn(db, ts.placement_id, ts.period_start, ts.period_end)
        except Exception:
            logger.exception("prefill source {} failed for timesheet {}", name, ts.id)
            continue
        for item in items:
            key = (item.date, item.category)
            if (
                key in manual
                or key in seen
                or item.category not in enabled
                or not ts.period_start <= item.date <= ts.period_end
                or not ZERO <= item.hours <= DAY_MAX
            ):
                continue
            seen.add(key)
            db.add(
                TimesheetEntry(
                    id=uuid.uuid4(),
                    tenant_id=ts.tenant_id,
                    timesheet_id=ts.id,
                    entry_date=item.date,
                    category=item.category,
                    hours=item.hours,
                    holiday_id=item.holiday_id,
                    leave_type_id=item.leave_type_id,
                    prefilled=True,
                    prefill_deletable=item.deletable,
                    note=item.label,
                )
            )
            written += 1
    await db.flush()
    return written
