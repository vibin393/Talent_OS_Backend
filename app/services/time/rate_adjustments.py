"""Retro rate adjustments (FR-132, AD-23): the ``RateSegmentAdded`` handler.

For each ledger (``billing``: bill rate; ``payable``: pay rate, or vendor rate for vendor payees) and each
``timesheet_entry`` / ``adjustment_entry`` dated in a superseded window with a **posted** posting, insert
``rate_delta = new_rate - (base_rate + sum of prior deltas)`` where ``base_rate`` is the rate the item was
originally posted at (the first superseded segment's rate). Idempotent per (ledger, source, segment); zero
deltas
are skipped; unposted items need none (consumers price them at the new segment). Only hourly segments carry
per-hour deltas. ``time`` stores no money: consumers price ``quantity x rate_delta x multiplier``.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.models.placements import Placement
from app.models.postings import BillingPosting, PayablePosting
from app.models.time import RateAdjustment, Timesheet, TimesheetAdjustmentEntry, TimesheetEntry
from app.services import placements_service
from app.services.time.common import dec
from app.services.time.configs import config_version
from app.services.time.events import RateAdjustmentsCreated

LEDGER_MODELS: dict[str, Any] = {"billing": BillingPosting, "payable": PayablePosting}


def _rate(seg: Any, ledger: str, payee_kind: str) -> Decimal | None:
    if ledger == "billing":
        return seg.bill_rate  # type: ignore[no-any-return]
    return seg.vendor_rate if payee_kind == "vendor" else seg.pay_rate  # type: ignore[no-any-return]


async def _items(
    db: AsyncSession, placement_id: uuid.UUID, start: date, end: date | None
) -> list[tuple[str, uuid.UUID, date, str, Decimal, uuid.UUID, int]]:
    """(source_kind, id, date, category, quantity, timesheet_id, config_version) of approved items in the
    window."""
    out: list[tuple[str, uuid.UUID, date, str, Decimal, uuid.UUID, int]] = []
    q1 = (
        select(TimesheetEntry, Timesheet.config_version)
        .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
        .where(
            Timesheet.placement_id == placement_id,
            Timesheet.status == "approved",
            TimesheetEntry.entry_date >= start,
            TimesheetEntry.hours > 0,
        )
    )
    q2 = (
        select(TimesheetAdjustmentEntry, Timesheet.config_version)
        .join(Timesheet, Timesheet.id == TimesheetAdjustmentEntry.timesheet_id)
        .where(
            Timesheet.placement_id == placement_id,
            Timesheet.status == "approved",
            TimesheetAdjustmentEntry.entry_date >= start,
        )
    )
    if end is not None:
        q1 = q1.where(TimesheetEntry.entry_date <= end)
        q2 = q2.where(TimesheetAdjustmentEntry.entry_date <= end)
    for e, version in (await db.execute(q1)).all():
        out.append(("timesheet_entry", e.id, e.entry_date, e.category, e.hours, e.timesheet_id, version))
    for a, version in (await db.execute(q2)).all():
        out.append(
            ("adjustment_entry", a.id, a.entry_date, a.category, a.delta_hours, a.timesheet_id, version)
        )
    return out


async def _posted(db: AsyncSession, ledger: str, kind: str, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    model = LEDGER_MODELS[ledger]
    rows = await db.execute(
        select(model.source_id).where(
            model.source_kind == kind, model.source_id.in_(ids), model.state == "posted"
        )
    )
    return set(rows.scalars())


async def handle_segment_added(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, segment_id: uuid.UUID, superseded: Any
) -> int:
    if not superseded:
        return 0
    placement = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if placement is None:
        return 0
    new_seg = await placements_service.get_rate_segment(ctx, db, segment_id)
    if new_seg.rate_unit != "hour":
        logger.info("rate segment {} is not hourly: no per-hour rate adjustments", segment_id)
        return 0
    created = 0
    multipliers: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for raw in superseded:
        old_id, cov_from, cov_to = raw[0], raw[1], raw[2]
        old_id = uuid.UUID(str(old_id))
        cov_from = cov_from if isinstance(cov_from, date) else date.fromisoformat(str(cov_from))
        cov_to = (
            None
            if cov_to is None
            else (cov_to if isinstance(cov_to, date) else date.fromisoformat(str(cov_to)))
        )
        old_seg = await placements_service.get_rate_segment(ctx, db, old_id)
        if old_seg.rate_unit != "hour":
            continue
        items = await _items(db, placement_id, cov_from, cov_to)
        if not items:
            continue
        for ledger in ("billing", "payable"):
            new_rate = _rate(new_seg, ledger, placement.payee_kind)
            old_rate = _rate(old_seg, ledger, placement.payee_kind)
            if new_rate is None or old_rate is None:
                continue
            by_kind: dict[str, list[uuid.UUID]] = defaultdict(list)
            for item in items:
                by_kind[item[0]].append(item[1])
            posted: set[uuid.UUID] = set()
            for kind, ids in by_kind.items():
                posted |= await _posted(db, ledger, kind, ids)
            if not posted:
                continue
            prior: dict[tuple[str, uuid.UUID], list[RateAdjustment]] = defaultdict(list)
            for row in (
                await db.execute(
                    select(RateAdjustment)
                    .where(RateAdjustment.ledger == ledger, RateAdjustment.original_source_id.in_(posted))
                    .order_by(RateAdjustment.created_at)
                )
            ).scalars():
                prior[row.original_source_kind, row.original_source_id].append(row)
            for kind, sid, d, cat, qty, _ts_id, version in items:
                if sid not in posted:
                    continue
                history = prior[kind, sid]
                if any(h.segment_id == segment_id for h in history):
                    continue
                base = Decimal(history[0].base_rate) if history else Decimal(old_rate)
                delta = Decimal(new_rate) - (base + sum((Decimal(h.rate_delta) for h in history), Decimal(0)))
                if delta == 0:
                    continue
                if version not in multipliers:
                    cfg = await config_version(db, placement_id, version)
                    multipliers[version] = (dict(cfg.bill_multipliers or {}), dict(cfg.pay_multipliers or {}))
                bill_m, pay_m = multipliers[version]
                mult = dec((bill_m if ledger == "billing" else pay_m).get(cat, "1"))
                stmt = (
                    pg_insert(RateAdjustment)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=placement.tenant_id,
                        ledger=ledger,
                        original_source_kind=kind,
                        original_source_id=sid,
                        placement_id=placement_id,
                        candidate_id=placement.candidate_id,
                        legal_entity_id=placement.legal_entity_id,
                        vendor_id=placement.vendor_id,
                        item_date=d,
                        category=cat,
                        quantity=qty,
                        base_rate=base,
                        new_rate=Decimal(new_rate),
                        rate_delta=delta,
                        multiplier=mult,
                        currency=placement.currency,
                        segment_id=segment_id,
                    )
                    .on_conflict_do_nothing(constraint="uq_rate_adjustments_source_segment")
                    .returning(RateAdjustment.id)
                )
                if (await db.execute(stmt)).scalar_one_or_none() is not None:
                    created += 1
    await db.flush()
    if created:
        emit(
            db,
            RateAdjustmentsCreated(
                tenant_id=placement.tenant_id, placement_id=placement_id, segment_id=segment_id, count=created
            ),
        )
    return created
