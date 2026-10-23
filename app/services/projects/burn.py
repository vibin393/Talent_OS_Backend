# ruff: file-ignore[line-too-long]
"""Budget burn read model (Epic 45, FR-230; AD-33 / AD-51 / AD-20).

Only posted facts count:

* ``hours``   -- entries (regular / OT / double time) of ``approved`` timesheets;
* ``cost``    -- ``payable_postings`` in state ``posted``;
* ``revenue`` -- lines of ``issued`` invoices (placement lines in the assignment window, plus the lines billing
  the project's milestones through ``posted`` milestone postings), minus lines of ``issued`` credit notes that
  reference them. Drafts, reserved / released postings, unapproved timesheets and cancelled invoices never count.

Each placement fact is attributed only inside an assignment window of the project and multiplied by that
assignment's ``allocation_pct / 100``. Money is converted to the project currency with
``legal_entities_service.rate_for`` at the fact date (rates recorded in the breakdown). ``compute`` is pure;
the ``source_hash`` makes a rebuild without new facts a no-op, so two rebuilds give identical Decimal rows.
Crossing 80 % / 100 % of a budget emits ``ProjectBudgetThresholdReached`` once per budget version.
"""

import dataclasses
import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.fiscal import local_date
from app.core.money import quantize_amount, quantize_hours
from app.events.projects import ProjectBudgetThresholdReached
from app.models.projects import Project, ProjectAssignment, ProjectBudget, ProjectBurn
from app.schemas.projects import BurnOut
from app.services.projects.common import DEFAULT_TZ, WRITE, get_project, now, tenant_of, today

HUNDRED = Decimal(100)
WORK_CATEGORIES = ("regular", "ot", "double_time")
DIMENSIONS = ("hours", "cost", "revenue")
PCT_CAP = Decimal("999.9999")


@dataclasses.dataclass(frozen=True, slots=True)
class Window:
    placement_id: uuid.UUID
    start: date
    end: date | None
    allocation_pct: Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class Fact:
    dimension: str
    placement_id: uuid.UUID | None  # None = project-level (milestone revenue)
    on: date
    amount: Decimal
    currency: str | None  # None for hours
    source: str
    window_free: bool = False  # milestone lines: not tied to an assignment window


@dataclasses.dataclass(frozen=True, slots=True)
class Figures:
    dimension: str
    actual: Decimal
    budget: Decimal | None
    pct: Decimal | None
    burn_rate_per_day: Decimal
    forecast_at_completion: Decimal
    currency: str | None
    breakdown: dict[str, Any]
    source_hash: str


Rates = dict[tuple[str, str, date], Decimal | None]


# ================================================================ pure core
def _window_for(windows: Sequence[Window], placement_id: uuid.UUID | None, on: date) -> Window | None:
    for w in windows:
        if w.placement_id == placement_id and w.start <= on and (w.end is None or on <= w.end):
            return w
    return None


def _q(value: Decimal, dimension: str, currency: str) -> Decimal:
    return quantize_hours(value) if dimension == "hours" else quantize_amount(value, currency)


def compute(
    dimension: str,
    facts: Iterable[Fact],
    windows: Sequence[Window],
    *,
    currency: str,
    rates: Rates,
    budget: Decimal | None,
    start: date | None,
    end: date | None,
    as_of: date,
) -> Figures:
    """Burn figures of one dimension from its facts (deterministic; no I/O)."""
    total = Decimal(0)
    by_key: dict[str, Decimal] = defaultdict(Decimal)
    used: dict[str, str] = {}
    missing: set[str] = set()
    for f in facts:
        if f.dimension != dimension:
            continue
        if f.window_free:
            factor = Decimal(1)
        else:
            w = _window_for(windows, f.placement_id, f.on)
            if w is None:
                continue
            factor = w.allocation_pct / HUNDRED
        amount = f.amount * factor
        if f.currency is not None and f.currency != currency:
            rate = rates.get((f.currency, currency, f.on))
            key = f"{f.currency}>{currency}@{f.on.isoformat()}"
            if rate is None:
                missing.add(key)
                continue
            used[key] = str(rate)
            amount = amount * rate
        total += amount
        by_key[f"{f.placement_id or 'project'}|{f.on.strftime('%Y-%m')}"] += amount
    actual = _q(total, dimension, currency)
    unit = None if dimension == "hours" else currency
    breakdown: dict[str, Any] = {
        "by_placement_month": {k: str(_q(v, dimension, currency)) for k, v in sorted(by_key.items())},
        "fx": dict(sorted(used.items())),
    }
    if missing:
        breakdown["fx_missing"] = sorted(missing)
    pct = None
    if budget is not None and budget > 0:
        pct = min((actual / budget * HUNDRED).quantize(Decimal("0.0001")), PCT_CAP)
    elapsed = max(1, (as_of - start).days + 1) if start else None
    rate_per_day = (
        _q(actual / Decimal(elapsed), dimension, currency) if elapsed else _q(Decimal(0), dimension, currency)
    )
    remaining = max(0, (end - as_of).days) if end else 0
    forecast = _q(actual + rate_per_day * Decimal(remaining), dimension, currency)
    material = json.dumps(
        {"actual": str(actual), "budget": str(budget), "breakdown": breakdown, "unit": unit}, sort_keys=True
    )
    return Figures(
        dimension=dimension,
        actual=actual,
        budget=budget,
        pct=pct,
        burn_rate_per_day=rate_per_day,
        forecast_at_completion=forecast,
        currency=unit,
        breakdown=breakdown,
        source_hash=hashlib.sha256(material.encode()).hexdigest(),
    )


# ================================================================ fact queries (read only)
def _local(value: datetime | None, fallback: date) -> date:
    return local_date(value, DEFAULT_TZ) if value is not None else fallback


async def windows_of(db: AsyncSession, project_id: uuid.UUID) -> list[Window]:
    rows = (
        await db.execute(select(ProjectAssignment).where(ProjectAssignment.project_id == project_id))
    ).scalars()
    return [Window(a.placement_id, a.start_date, a.end_date, a.allocation_pct) for a in rows]


async def milestone_line_ids(db: AsyncSession, project: Project) -> set[uuid.UUID]:
    """Invoice lines billing the project's SOW milestones (posted milestone postings)."""
    from app.models.commercials import ContractMilestone
    from app.models.postings import BillingPosting

    if project.contract_id is None:
        return set()
    rows = await db.execute(
        select(BillingPosting.target_id)
        .join(ContractMilestone, ContractMilestone.id == BillingPosting.source_id)
        .where(
            BillingPosting.source_kind == "milestone",
            BillingPosting.state == "posted",
            BillingPosting.target_kind == "invoice_line",
            ContractMilestone.contract_id == project.contract_id,
        )
    )
    return set(rows.scalars())


async def collect(db: AsyncSession, project: Project, windows: Sequence[Window], as_of: date) -> list[Fact]:
    from app.models.billing import CreditNote, CreditNoteLine, Invoice, InvoiceLine
    from app.models.postings import PayablePosting
    from app.models.time import Timesheet, TimesheetEntry

    placements = sorted({w.placement_id for w in windows}, key=str)
    facts: list[Fact] = []
    if placements:
        for on, hours, pid in (
            await db.execute(
                select(TimesheetEntry.entry_date, TimesheetEntry.hours, Timesheet.placement_id)
                .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
                .where(
                    Timesheet.status == "approved",
                    Timesheet.placement_id.in_(placements),
                    TimesheetEntry.category.in_(WORK_CATEGORIES),
                    TimesheetEntry.entry_date <= as_of,
                )
            )
        ).all():
            facts.append(Fact("hours", pid, on, Decimal(hours), None, "timesheet"))
        for posted_at, amount, ccy, pid in (
            await db.execute(
                select(
                    PayablePosting.posted_at,
                    PayablePosting.amount,
                    PayablePosting.currency,
                    PayablePosting.placement_id,
                ).where(PayablePosting.state == "posted", PayablePosting.placement_id.in_(placements))
            )
        ).all():
            on = _local(posted_at, as_of)
            if on <= as_of:
                facts.append(Fact("cost", pid, on, Decimal(amount), ccy, "payable_posting"))
    milestone_lines = await milestone_line_ids(db, project)
    line_meta: dict[uuid.UUID, tuple[uuid.UUID | None, date, bool]] = {}
    conds = []
    if placements:
        conds.append(InvoiceLine.placement_id.in_(placements))
    if milestone_lines:
        conds.append(InvoiceLine.id.in_(milestone_lines))
    if conds:
        from sqlalchemy import or_

        for line_id, pid, p_start, p_end, doc_date, value, ccy in (
            await db.execute(
                select(
                    InvoiceLine.id,
                    InvoiceLine.placement_id,
                    InvoiceLine.period_start,
                    InvoiceLine.period_end,
                    Invoice.doc_date,
                    InvoiceLine.taxable_value,
                    Invoice.currency,
                )
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.status == "issued", or_(*conds))
            )
        ).all():
            on = p_end or p_start or doc_date or as_of
            free = line_id in milestone_lines
            line_meta[line_id] = (None if free else pid, on, free)
            facts.append(
                Fact("revenue", None if free else pid, on, Decimal(value), ccy, "invoice_line", free)
            )
    if line_meta:
        for orig, value, ccy in (
            await db.execute(
                select(
                    CreditNoteLine.original_invoice_line_id, CreditNoteLine.taxable_value, CreditNote.currency
                )
                .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
                .where(
                    CreditNote.status == "issued",
                    CreditNoteLine.original_invoice_line_id.in_(list(line_meta)),
                )
            )
        ).all():
            pid, on, free = line_meta[orig]
            facts.append(Fact("revenue", pid, on, -Decimal(value), ccy, "credit_note_line", free))
    return facts


async def fx_rates(ctx: RequestContext, db: AsyncSession, facts: Iterable[Fact], currency: str) -> Rates:
    from app.core.errors import ValidationFailed
    from app.services import legal_entities_service

    out: Rates = {}
    for f in facts:
        if f.currency is None or f.currency == currency:
            continue
        key = (f.currency, currency, f.on)
        if key in out:
            continue
        try:
            rate = await legal_entities_service.rate_for(ctx, db, f.currency, currency, f.on)
            out[key] = Decimal(rate.rate)
        except ValidationFailed:
            out[key] = None
    return out


# ================================================================ rebuild
def _burn_out(row: ProjectBurn) -> BurnOut:
    return BurnOut(
        dimension=row.dimension,  # type: ignore[arg-type]
        actual=row.actual,
        budget=row.budget,
        pct=row.pct,
        burn_rate_per_day=row.burn_rate_per_day,
        forecast_at_completion=row.forecast_at_completion,
        currency=row.currency,
        as_of=row.as_of,
        breakdown=row.breakdown,
    )


async def burn_rows(db: AsyncSession, project_id: uuid.UUID) -> list[ProjectBurn]:
    return list(
        (
            await db.execute(
                select(ProjectBurn)
                .where(ProjectBurn.project_id == project_id)
                .order_by(ProjectBurn.dimension)
            )
        ).scalars()
    )


async def rebuild_project(
    ctx: RequestContext, db: AsyncSession, project: Project, as_of: date | None = None
) -> list[ProjectBurn]:
    as_of = as_of or today()
    currency = project.currency or "INR"
    windows = await windows_of(db, project.id)
    facts = await collect(db, project, windows, as_of)
    rates = await fx_rates(ctx, db, facts, currency)
    budgets = {
        b.dimension: b
        for b in (
            await db.execute(
                select(ProjectBudget).where(ProjectBudget.project_id == project.id).with_for_update()
            )
        ).scalars()
    }
    existing = {r.dimension: r for r in await burn_rows(db, project.id)}
    for dimension in DIMENSIONS:
        budget = budgets.get(dimension)
        fig = compute(
            dimension,
            facts,
            windows,
            currency=currency,
            rates=rates,
            budget=budget.amount if budget else None,
            start=project.start_date,
            end=project.end_date,
            as_of=as_of,
        )
        row = existing.get(dimension)
        if row is None:
            row = ProjectBurn(
                id=uuid.uuid4(), tenant_id=tenant_of(db), project_id=project.id, dimension=dimension
            )
            db.add(row)
            existing[dimension] = row
        elif row.source_hash == fig.source_hash and row.as_of == as_of:
            await _alerts(db, project, budget, row.pct)
            continue
        row.actual = fig.actual
        row.budget = fig.budget
        row.pct = fig.pct
        row.burn_rate_per_day = fig.burn_rate_per_day
        row.forecast_at_completion = fig.forecast_at_completion
        row.currency = fig.currency
        row.as_of = as_of
        row.breakdown = fig.breakdown
        row.source_hash = fig.source_hash
        await _alerts(db, project, budget, fig.pct)
    await db.flush()
    return await burn_rows(db, project.id)


async def _alerts(
    db: AsyncSession, project: Project, budget: ProjectBudget | None, pct: Decimal | None
) -> None:
    if budget is None or pct is None:
        return
    for threshold, attr in ((80, "alert_80_at"), (100, "alert_100_at")):
        if pct >= Decimal(threshold) and getattr(budget, attr) is None:
            setattr(budget, attr, now())
            emit(
                db,
                ProjectBudgetThresholdReached(
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                    dimension=budget.dimension,
                    threshold=threshold,
                    budget_version=budget.version,
                ),
            )


async def rebuild(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> list[BurnOut]:
    project = await get_project(ctx, db, project_id, permission=WRITE, lock=True)
    return [_burn_out(r) for r in await rebuild_project(ctx, db, project)]


async def list_burn(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> list[BurnOut]:
    await get_project(ctx, db, project_id)
    return [_burn_out(r) for r in await burn_rows(db, project_id)]


async def rebuild_all(ctx: RequestContext, db: AsyncSession) -> int:
    """Beat: every open project of the tenant."""
    ids = list(
        (
            await db.execute(select(Project.id).where(Project.status.in_(("active", "on_hold", "completed"))))
        ).scalars()
    )
    for pid in ids:
        project = (await db.execute(select(Project).where(Project.id == pid).with_for_update())).scalar_one()
        await rebuild_project(ctx, db, project)
    return len(ids)


async def projects_for(db: AsyncSession, kind: str, ref_id: uuid.UUID) -> list[uuid.UUID]:
    """Projects touched by a fact source event (``placement`` / ``invoice``)."""
    from app.models.billing import InvoiceLine
    from app.models.commercials import ContractMilestone
    from app.models.postings import BillingPosting

    if kind == "placement":
        placement_ids = [ref_id]
    else:
        placement_ids = [
            p
            for p in (
                await db.execute(select(InvoiceLine.placement_id).where(InvoiceLine.invoice_id == ref_id))
            ).scalars()
            if p
        ]
    ids: set[uuid.UUID] = set()
    if placement_ids:
        ids |= set(
            (
                await db.execute(
                    select(ProjectAssignment.project_id).where(
                        ProjectAssignment.placement_id.in_(placement_ids)
                    )
                )
            ).scalars()
        )
    if kind == "invoice":
        contracts = set(
            (
                await db.execute(
                    select(ContractMilestone.contract_id)
                    .join(BillingPosting, BillingPosting.source_id == ContractMilestone.id)
                    .where(BillingPosting.source_kind == "milestone", BillingPosting.document_id == ref_id)
                )
            ).scalars()
        )
        if contracts:
            ids |= set(
                (await db.execute(select(Project.id).where(Project.contract_id.in_(contracts)))).scalars()
            )
    return sorted(ids, key=str)


async def rebuild_many(ctx: RequestContext, db: AsyncSession, project_ids: Sequence[uuid.UUID]) -> int:
    n = 0
    for pid in project_ids:
        project = (
            await db.execute(select(Project).where(Project.id == pid).with_for_update())
        ).scalar_one_or_none()
        if project is not None:
            await rebuild_project(ctx, db, project)
            n += 1
    return n
