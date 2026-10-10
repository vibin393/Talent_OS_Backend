"""Pull-only posting sources of the ``time`` domain (AD-23): ``timesheet_entry``, ``adjustment_entry`` and
``rate_adjustment``. Loaders return items of **approved** Timesheets only; they never write and ``time`` never
pushes lines. Billing / payroll / vendor statements call ``postings.load_unposted`` which excludes items
already
active in the ledger. ``amount`` is ``None`` (time stores no money); quantities are Decimal hours (signed for
adjustments). Registered from ``hooks.py``.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.placements import Placement
from app.models.time import RateAdjustment, Timesheet, TimesheetAdjustmentEntry, TimesheetEntry
from app.services.postings import SourceItem, SourceQuery
from app.services.time.common import dec
from app.services.time.configs import config_version

ENTRY = "timesheet_entry"
ADJUSTMENT = "adjustment_entry"
RATE_ADJUSTMENT = "rate_adjustment"


def _filters(stmt: Any, date_col: Any, query: SourceQuery, approved_before: datetime | None) -> Any:
    stmt = stmt.where(Timesheet.status == "approved", date_col <= query.period_end)
    if query.period_start is not None:
        stmt = stmt.where(date_col >= query.period_start)
    if query.placement_ids is not None:
        stmt = stmt.where(Timesheet.placement_id.in_(list(query.placement_ids)))
    if query.legal_entity_id is not None:
        stmt = stmt.where(Timesheet.legal_entity_id == query.legal_entity_id)
    if query.payee_kind is not None:
        stmt = stmt.where(Placement.payee_kind == query.payee_kind)
    if approved_before is not None:
        stmt = stmt.where(Timesheet.approved_at < approved_before)
    return stmt


class _Multipliers:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.cache: dict[tuple[uuid.UUID, int], tuple[dict[str, Any], dict[str, Any]]] = {}

    async def get(self, placement_id: uuid.UUID, version: int) -> tuple[dict[str, Any], dict[str, Any]]:
        key = (placement_id, version)
        if key not in self.cache:
            cfg = await config_version(self.db, placement_id, version)
            self.cache[key] = (dict(cfg.bill_multipliers or {}), dict(cfg.pay_multipliers or {}))
        return self.cache[key]


def _attrs(ts: Timesheet, category: str, bill: dict[str, Any], pay: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": category,
        "billable": dec(bill.get(category, "0")) > 0,
        "payable": dec(pay.get(category, "0")) > 0,
        "candidate_id": ts.candidate_id,
        "legal_entity_id": ts.legal_entity_id,
        "vendor_id": ts.vendor_id,
        "approved_at": ts.approved_at,
        "unit": "hour",
    }


async def load_entries(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    query: SourceQuery,
    *,
    approved_before: datetime | None = None,
) -> list[SourceItem]:
    stmt = (
        select(TimesheetEntry, Timesheet, Placement.currency)
        .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
        .join(Placement, Placement.id == Timesheet.placement_id)
        .where(Timesheet.kind == "regular", TimesheetEntry.hours > 0)
        .order_by(TimesheetEntry.entry_date, TimesheetEntry.id)
    )
    stmt = _filters(stmt, TimesheetEntry.entry_date, query, approved_before)
    mult = _Multipliers(db)
    out: list[SourceItem] = []
    for e, ts, currency in (await db.execute(stmt)).all():
        bill, pay = await mult.get(ts.placement_id, ts.config_version)
        out.append(
            SourceItem(
                ENTRY,
                e.id,
                ts.placement_id,
                e.entry_date,
                Decimal(e.hours),
                None,
                currency,
                _attrs(ts, e.category, bill, pay),
            )
        )
    return out


async def load_adjustments(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    query: SourceQuery,
    *,
    approved_before: datetime | None = None,
) -> list[SourceItem]:
    stmt = (
        select(TimesheetAdjustmentEntry, Timesheet, Placement.currency)
        .join(Timesheet, Timesheet.id == TimesheetAdjustmentEntry.timesheet_id)
        .join(Placement, Placement.id == Timesheet.placement_id)
        .where(Timesheet.kind == "adjustment")
        .order_by(TimesheetAdjustmentEntry.entry_date, TimesheetAdjustmentEntry.id)
    )
    stmt = _filters(stmt, TimesheetAdjustmentEntry.entry_date, query, approved_before)
    mult = _Multipliers(db)
    out: list[SourceItem] = []
    for a, ts, currency in (await db.execute(stmt)).all():
        bill, pay = await mult.get(ts.placement_id, ts.config_version)
        attrs = _attrs(ts, a.category, bill, pay)
        attrs["original_source_kind"] = "timesheet"
        attrs["original_source_id"] = a.original_timesheet_id
        out.append(
            SourceItem(
                ADJUSTMENT, a.id, ts.placement_id, a.entry_date, Decimal(a.delta_hours), None, currency, attrs
            )
        )
    return out


async def load_rate_adjustments(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    query: SourceQuery,
) -> list[SourceItem]:
    stmt = (
        select(RateAdjustment, Placement.payee_kind)
        .join(Placement, Placement.id == RateAdjustment.placement_id)
        .where(RateAdjustment.ledger == query.ledger, RateAdjustment.item_date <= query.period_end)
        .order_by(RateAdjustment.item_date, RateAdjustment.id)
    )
    if query.period_start is not None:
        stmt = stmt.where(RateAdjustment.item_date >= query.period_start)
    if query.placement_ids is not None:
        stmt = stmt.where(RateAdjustment.placement_id.in_(list(query.placement_ids)))
    if query.legal_entity_id is not None:
        stmt = stmt.where(RateAdjustment.legal_entity_id == query.legal_entity_id)
    if query.payee_kind is not None:
        stmt = stmt.where(Placement.payee_kind == query.payee_kind)
    out: list[SourceItem] = []
    for r, _payee in (await db.execute(stmt)).all():
        out.append(
            SourceItem(
                RATE_ADJUSTMENT,
                r.id,
                r.placement_id,
                r.item_date,
                Decimal(r.quantity),
                None,
                r.currency,
                {
                    "category": r.category,
                    "billable": r.ledger == "billing",
                    "payable": r.ledger == "payable",
                    "candidate_id": r.candidate_id,
                    "legal_entity_id": r.legal_entity_id,
                    "vendor_id": r.vendor_id,
                    "unit": "hour",
                    "rate_delta": Decimal(r.rate_delta),
                    "original_source_kind": r.original_source_kind,
                    "original_source_id": r.original_source_id,
                    "multiplier": Decimal(r.multiplier),
                },
            )
        )
    return out


def register() -> None:
    from app.services.postings import posting_sources, register_posting_source

    both = frozenset({"billing", "payable"})
    for kind, loader in (
        (ENTRY, load_entries),
        (ADJUSTMENT, load_adjustments),
        (RATE_ADJUSTMENT, load_rate_adjustments),
    ):
        if kind not in posting_sources():
            register_posting_source(kind, loader, ledgers=both)  # type: ignore[arg-type]
