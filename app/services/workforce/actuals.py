# ruff: noqa: E501
"""Plan vs actual (Epic 44, FR-225).

Rows by effective line (``group_by=line``) or by planned start month (``group_by=month``; every month of the plan
period, lines attributed to their start month): planned headcount (approved revision), slot counts, budget,
committed cost (``Decimal``) and variance. Committed cost per slot (``cost.py``; pro-rated to the months left in the
plan period from the start date):

* an accepted Offer of the slot's Demand whose employment type is ``permanent`` -> annual CTC x months / 12;
* else an active Placement of the Demand -> the active rate segment (pay rate, else bill rate) per month (standard
  hours / days from the workforce settings) x months; a ``fixed`` segment counts once;
* else an accepted (contract) Offer -> its pay rate per month x months;
* a Demand without Offer / Placement -> 0 with ``pending_commitment = true``.

Other currencies are converted with ``legal_entities.fx.rate_for`` at the start date (AD-20); a missing rate flags
``fx_missing`` and leaves the amount out. Demands / Placements are counted whatever the viewer's demand scope (counts
only); money needs ``compensation:view`` (else ``budget`` / ``committed`` / ``variance`` are ``None`` and
``money_masked`` is true). ``demand_ids`` let the UI read Epic 48 time-to-fill predictions (no analytics import).
"""

import dataclasses
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import ActualRow, ActualsOut
from app.services.workforce import cost
from app.services.workforce.common import (
    READ,
    add_months,
    can_view_money,
    effective_lines,
    get_plan,
    month_key,
    settings_row,
)

ACTIVE_PLACEMENT = ("pre_joining", "joining_confirmed", "active", "offboarding")


@dataclasses.dataclass
class SlotCost:
    amount: Decimal = Decimal(0)
    pending: bool = False
    fx_missing: bool = False


@dataclasses.dataclass
class _Acc:
    planned: int = 0
    requisitioned: int = 0
    filled: int = 0
    open: int = 0
    cancelled: int = 0
    budget: Decimal = Decimal(0)
    has_budget: bool = False
    committed: Decimal = Decimal(0)
    pending: bool = False
    fx_missing: bool = False
    demand_ids: list[uuid.UUID] = dataclasses.field(default_factory=list)

    def add(self, other: "_Acc") -> None:
        for f in ("planned", "requisitioned", "filled", "open", "cancelled"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.budget += other.budget
        self.has_budget = self.has_budget or other.has_budget
        self.committed += other.committed
        self.pending = self.pending or other.pending
        self.fx_missing = self.fx_missing or other.fx_missing
        self.demand_ids += other.demand_ids


async def _to_plan_currency(
    ctx: RequestContext, db: AsyncSession, amount: Decimal, ccy: str, plan: WorkforcePlan, on: date
) -> Decimal | None:
    if ccy.upper() == plan.currency.upper():
        return amount
    from app.services import legal_entities_service

    try:
        fx = await legal_entities_service.rate_for(ctx, db, ccy, plan.currency, on)
    except ValidationFailed:
        return None
    return cost.convert(amount, Decimal(fx.rate))


async def slot_cost(
    ctx: RequestContext,
    db: AsyncSession,
    plan: WorkforcePlan,
    slot: HeadcountSlot,
    *,
    hours: Decimal,
    days: Decimal,
) -> SlotCost:
    from app.models.offers import Offer, OfferVersion
    from app.models.placements import Placement, RateSegment

    if slot.demand_id is None or slot.status == "cancelled":
        return SlotCost()
    offer_version = (
        await db.execute(
            select(OfferVersion)
            .join(Offer, OfferVersion.id == Offer.released_version_id)
            .where(
                Offer.demand_id == slot.demand_id,
                Offer.status == "accepted",
                Offer.placement_dropped_at.is_(None),
            )
            .order_by(Offer.responded_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    placement = None
    if slot.placement_id is not None:
        placement = await db.get(Placement, slot.placement_id)
    if placement is None:
        placement = (
            await db.execute(
                select(Placement)
                .where(Placement.demand_id == slot.demand_id, Placement.status.in_(ACTIVE_PLACEMENT))
                .order_by(Placement.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def finish(amount: Decimal, ccy: str, on: date) -> SlotCost:
        converted = await _to_plan_currency(ctx, db, amount, ccy, plan, on)
        return SlotCost(fx_missing=True) if converted is None else SlotCost(amount=converted)

    if offer_version is not None and offer_version.employment_type == "permanent":
        start = offer_version.start_date
        annual = cost.annualise(Decimal(offer_version.pay_rate), offer_version.pay_rate_unit, hours, days)
        return await finish(
            cost.permanent_commitment(annual, cost.months_remaining(start, plan.period_end)),
            offer_version.pay_currency,
            start,
        )
    if placement is not None:
        segment = (
            await db.execute(
                select(RateSegment)
                .where(RateSegment.placement_id == placement.id, RateSegment.status == "active")
                .order_by(RateSegment.effective_from.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rate = (
            None
            if segment is None
            else (segment.pay_rate if segment.pay_rate is not None else segment.bill_rate)
        )
        if segment is not None and rate is not None:
            start = placement.actual_start_date or placement.planned_start_date
            per_month = cost.monthly(Decimal(rate), segment.rate_unit, hours, days)
            amount = (
                cost.q2(Decimal(rate))
                if per_month is None
                else cost.contract_commitment(per_month, cost.months_remaining(start, plan.period_end))
            )
            return await finish(amount, segment.currency, start)
    if offer_version is not None:
        start = offer_version.start_date
        per_month = cost.monthly(Decimal(offer_version.pay_rate), offer_version.pay_rate_unit, hours, days)
        if per_month is not None:
            return await finish(
                cost.contract_commitment(per_month, cost.months_remaining(start, plan.period_end)),
                offer_version.pay_currency,
                start,
            )
    return SlotCost(pending=True)


def _months(plan: WorkforcePlan) -> list[str]:
    out: list[str] = []
    cursor = plan.period_start.replace(day=1)
    while cursor <= plan.period_end:
        out.append(month_key(cursor))
        cursor = add_months(cursor, 1)
    return out


def _row(key: str, acc: _Acc, *, money: bool, **extra: Any) -> ActualRow:
    budget = cost.q2(acc.budget) if acc.has_budget else None
    committed = cost.q2(acc.committed)
    return ActualRow(
        key=key,
        planned=acc.planned,
        requisitioned=acc.requisitioned,
        filled=acc.filled,
        open=acc.open,
        cancelled=acc.cancelled,
        budget=budget if money else None,
        committed=committed if money else None,
        variance=(cost.q2(budget - committed) if budget is not None else None) if money else None,
        pending_commitment=acc.pending,
        fx_missing=acc.fx_missing,
        demand_ids=acc.demand_ids,
        **extra,
    )


async def plan_vs_actual(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, *, group_by: str = "line"
) -> ActualsOut:
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    money = can_view_money(ctx)
    settings = await settings_row(db)
    hours, days = Decimal(settings.standard_hours_per_month), Decimal(settings.standard_days_per_month)
    lines = await effective_lines(db, plan)
    lineages = [ln.lineage_id or ln.id for ln in lines]
    slots_by: dict[uuid.UUID, list[HeadcountSlot]] = defaultdict(list)
    if lineages:
        for s in (
            await db.execute(
                select(HeadcountSlot)
                .where(HeadcountSlot.plan_line_id.in_(lineages))
                .order_by(HeadcountSlot.seq)
            )
        ).scalars():
            slots_by[s.plan_line_id].append(s)
    per_line: list[tuple[WorkforcePlanLine, _Acc]] = []
    for ln in lines:
        acc = _Acc(
            planned=ln.headcount,
            budget=ln.budget_amount or Decimal(0),
            has_budget=ln.budget_amount is not None,
        )
        for s in slots_by[ln.lineage_id or ln.id]:
            setattr(acc, s.status, getattr(acc, s.status) + 1)
            if s.demand_id is not None:
                acc.demand_ids.append(s.demand_id)
            if s.demand_id is not None and s.status != "cancelled":
                c = await slot_cost(ctx, db, plan, s, hours=hours, days=days)
                acc.committed += c.amount
                acc.pending = acc.pending or c.pending
                acc.fx_missing = acc.fx_missing or c.fx_missing
        per_line.append((ln, acc))
    totals = _Acc()
    rows: list[ActualRow] = []
    if group_by == "month":
        buckets: dict[str, _Acc] = {m: _Acc() for m in _months(plan)}
        for ln, acc in per_line:
            buckets.setdefault(month_key(ln.period_start), _Acc()).add(acc)
        for m, acc in sorted(buckets.items()):
            totals.add(acc)
            rows.append(_row(m, acc, money=money, month=m))
    else:
        for ln, acc in per_line:
            totals.add(acc)
            rows.append(
                _row(
                    str(ln.lineage_id or ln.id),
                    acc,
                    money=money,
                    line_id=ln.id,
                    lineage_id=ln.lineage_id or ln.id,
                    role_title=ln.role_title,
                    month=month_key(ln.period_start),
                )
            )
    return ActualsOut(
        plan_id=plan.id,
        group_by="month" if group_by == "month" else "line",
        currency=plan.currency,
        money_masked=not money,
        effective_revision=plan.approved_revision,
        rows=rows,
        totals=_row("total", totals, money=money),
    )


EXPORT_COLUMNS = (
    ("Line / month", "key"),
    ("Role", "role_title"),
    ("Start month", "month"),
    ("Planned", "planned"),
    ("Requisitioned", "requisitioned"),
    ("Filled", "filled"),
    ("Open", "open"),
    ("Cancelled", "cancelled"),
    ("Budget", "budget"),
    ("Committed", "committed"),
    ("Variance", "variance"),
    ("Pending commitment", "pending_commitment"),
)


async def export_xlsx(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, *, group_by: str = "line"
) -> tuple[bytes, str]:
    """XLSX of ``plan_vs_actual`` through the P1 export writer; masked money stays empty; audited."""
    from app.services.reporting.export import XlsxWriter

    out = await plan_vs_actual(ctx, db, plan_id, group_by=group_by)
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    writer = XlsxWriter("Plan vs actual", [h for h, _ in EXPORT_COLUMNS])
    for row in [*out.rows, out.totals]:
        data = row.model_dump()
        values: list[Any] = []
        for _, key in EXPORT_COLUMNS:
            value = data.get(key)
            if key == "role_title" and row.key == "total":
                value = "Total"
            if key == "key" and group_by == "line" and row.key != "total":
                value = row.role_title
            if isinstance(value, bool):
                value = "yes" if value else ""
            values.append(value)
        writer.append(values)
    audit.record(
        "workforce.actuals_exported",
        "workforce_plan",
        plan.id,
        session=db,
        group_by=group_by,
        masked=out.money_masked,
    )
    safe = "".join(ch if ch.isalnum() else "-" for ch in plan.name)[:40].strip("-") or "plan"
    return writer.close(), f"workforce-{safe}-{group_by}.xlsx"
