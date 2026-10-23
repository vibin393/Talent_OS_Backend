# ruff: file-ignore[line-too-long, lowercase-imported-as-non-lowercase, ambiguous-unicode-character-docstring]
"""Posted-fact query builders (Epic 34, A2, AD-33): one definition per finance / ops figure.

Every money figure is read from **issued documents** (frozen per-line INR equivalents, AD-20) and summed in SQL
as ``NUMERIC``; Python only sees ``Decimal``. Nothing here prices, taxes, prorates or computes an open balance.

* Revenue (ex-GST) = Σ Invoice line ``inr_taxable_value`` (header ``issued``, or ``cancelled`` with
  ``cancel_basis='full_credit_note'``; ``is_imported=false``) + Σ issued Debit Note lines − Σ issued Credit Note
  lines, each by its own ``doc_date``. IRN-cancelled documents are excluded entirely.
* Invoiced incl. tax: the same set of headers using ``inr_document_total`` (display only).
* Cost: Σ frozen ``cost_amount_inr`` over the same lines (notes signed alike). Margin = revenue − cost.
* Collections: Σ Payment ``inr_amount`` by ``payment_date`` − Σ reversed Payments by ``reversal_date``.
* Unallocated: Σ ``client_credits.amount`` with a payment source, as of ``as_of``.

Spec column names are adapted to the merged billing schema: ``taxable_inr`` -> ``inr_taxable_value``,
``document_total_inr`` -> ``inr_document_total``, ``imported`` -> ``is_imported``.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Date, Numeric, and_, cast, distinct, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reporting import p2tables as T

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def q2(value: Any) -> Decimal:
    """``Decimal`` quantized to paise (half-up); None -> 0.00."""
    return (Decimal(value) if value is not None else ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Any, currency: str = "INR") -> dict[str, str]:
    """AD-12 money shape: exact decimal string + currency."""
    return {"amount": f"{q2(value):.2f}", "currency": currency}


def margin_pct(revenue: Decimal, margin: Decimal) -> Decimal | None:
    if revenue == 0:
        return None
    return (margin / revenue * 100).quantize(CENT, rounding=ROUND_HALF_UP)


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(month: date) -> date:
    nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def add_months(month: date, n: int) -> date:
    y, m = divmod(month.month - 1 + n, 12)
    return date(month.year + y, m + 1, 1)


def months_between(first: date, last: date) -> list[date]:
    out, cur = [], month_start(first)
    while cur <= month_start(last):
        out.append(cur)
        cur = add_months(cur, 1)
    return out


# ================================================================ document sets
def _invoice_counted() -> Any:
    """Invoices that count for revenue (issued, or cancelled by a full Credit Note), never imported."""
    i = T.invoices.c
    return and_(
        or_(i.status == "issued", and_(i.status == "cancelled", i.cancel_basis == "full_credit_note")),
        i.is_imported.is_(False),
    )


def _doc_filter(
    le_col: Any, date_col: Any, le_ids: Sequence[uuid.UUID], start: date | None, end: date | None
) -> list[Any]:
    conds: list[Any] = []
    if le_ids:
        conds.append(le_col.in_(list(le_ids)))
    if start is not None:
        conds.append(date_col >= start)
    if end is not None:
        conds.append(date_col <= end)
    return conds


def line_facts(le_ids: Sequence[uuid.UUID] = (), start: date | None = None, end: date | None = None) -> Any:
    """Signed revenue / cost lines: ``legal_entity_id, client_id, placement_id, doc_date, taxable, cost, src``."""
    i, il = T.invoices.c, T.invoice_lines.c
    c, cl = T.credit_notes.c, T.credit_note_lines.c
    d, dl = T.debit_notes.c, T.debit_note_lines.c
    inv = (
        select(
            i.legal_entity_id, i.client_id, il.placement_id, i.doc_date,
            il.inr_taxable_value.label("taxable"), il.cost_amount_inr.label("cost"), literal("invoice").label("src"),
        )
        .select_from(T.invoice_lines.join(T.invoices, i.id == il.invoice_id))
        .where(_invoice_counted(), *_doc_filter(i.legal_entity_id, i.doc_date, le_ids, start, end))
    )  # fmt: skip
    dn = (
        select(
            d.legal_entity_id, d.client_id, dl.placement_id, d.doc_date,
            dl.inr_taxable_value.label("taxable"), dl.cost_amount_inr.label("cost"), literal("debit").label("src"),
        )
        .select_from(T.debit_note_lines.join(T.debit_notes, d.id == dl.debit_note_id))
        .where(d.status == "issued", *_doc_filter(d.legal_entity_id, d.doc_date, le_ids, start, end))
    )  # fmt: skip
    cn = (
        select(
            c.legal_entity_id, c.client_id, cl.placement_id, c.doc_date,
            (-cl.inr_taxable_value).label("taxable"), (-cl.cost_amount_inr).label("cost"), literal("credit").label("src"),
        )
        .select_from(T.credit_note_lines.join(T.credit_notes, c.id == cl.credit_note_id))
        .where(c.status == "issued", *_doc_filter(c.legal_entity_id, c.doc_date, le_ids, start, end))
    )  # fmt: skip
    return union_all(inv, dn, cn).subquery("lf")


def header_facts(le_ids: Sequence[uuid.UUID] = (), start: date | None = None, end: date | None = None) -> Any:
    """Signed document totals incl. tax: ``legal_entity_id, client_id, doc_date, total, src``."""
    i, c, d = T.invoices.c, T.credit_notes.c, T.debit_notes.c
    inv = select(
        i.legal_entity_id,
        i.client_id,
        i.doc_date,
        i.inr_document_total.label("total"),
        literal("invoice").label("src"),
    ).where(_invoice_counted(), *_doc_filter(i.legal_entity_id, i.doc_date, le_ids, start, end))
    dn = select(
        d.legal_entity_id,
        d.client_id,
        d.doc_date,
        d.inr_document_total.label("total"),
        literal("debit").label("src"),
    ).where(d.status == "issued", *_doc_filter(d.legal_entity_id, d.doc_date, le_ids, start, end))
    cn = select(
        c.legal_entity_id,
        c.client_id,
        c.doc_date,
        (-c.inr_document_total).label("total"),
        literal("credit").label("src"),
    ).where(c.status == "issued", *_doc_filter(c.legal_entity_id, c.doc_date, le_ids, start, end))
    return union_all(inv, dn, cn).subquery("hf")


# ================================================================ finance figures
@dataclasses.dataclass(frozen=True, slots=True)
class FinanceRow:
    revenue: Decimal = ZERO
    invoiced_with_tax: Decimal = ZERO
    cost: Decimal = ZERO
    credit: Decimal = ZERO
    debit: Decimal = ZERO
    invoice_count: int = 0
    credit_note_count: int = 0

    @property
    def margin(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def margin_pct(self) -> Decimal | None:
        return margin_pct(self.revenue, self.margin)


FinanceKey = tuple[uuid.UUID, date, uuid.UUID]  # (legal_entity_id, month, client_id)
FIN_FIELDS = ("revenue", "invoiced_with_tax", "cost", "credit", "debit", "invoice_count", "credit_note_count")


async def finance_by_month(
    db: AsyncSession, le_ids: Sequence[uuid.UUID] = (), start: date | None = None, end: date | None = None
) -> dict[FinanceKey, FinanceRow]:
    """Live finance facts per (LE, month, client) for documents dated in ``[start, end]``."""
    lf = line_facts(le_ids, start, end)
    month = cast(func.date_trunc("month", lf.c.doc_date), Date)
    line_rows = await db.execute(
        select(
            lf.c.legal_entity_id, month.label("m"), lf.c.client_id,
            func.coalesce(func.sum(lf.c.taxable), 0),
            func.coalesce(func.sum(lf.c.cost), 0),
            func.coalesce(func.sum(lf.c.taxable).filter(lf.c.src == "credit"), 0),
            func.coalesce(func.sum(lf.c.taxable).filter(lf.c.src == "debit"), 0),
        ).group_by(lf.c.legal_entity_id, month, lf.c.client_id)
    )  # fmt: skip
    acc: dict[FinanceKey, dict[str, Any]] = {}
    for le, m, client, revenue, cost, credit, debit in line_rows.tuples():
        acc[le, m, client] = {
            "revenue": q2(revenue), "cost": q2(cost), "credit": q2(-Decimal(credit)), "debit": q2(debit)
        }  # fmt: skip
    hf = header_facts(le_ids, start, end)
    hmonth = cast(func.date_trunc("month", hf.c.doc_date), Date)
    head_rows = await db.execute(
        select(
            hf.c.legal_entity_id, hmonth.label("m"), hf.c.client_id,
            func.coalesce(func.sum(hf.c.total), 0),
            func.count().filter(hf.c.src == "invoice"),
            func.count().filter(hf.c.src == "credit"),
        ).group_by(hf.c.legal_entity_id, hmonth, hf.c.client_id)
    )  # fmt: skip
    for le, m, client, total, inv_n, cn_n in head_rows.tuples():
        row = acc.setdefault((le, m, client), {})
        row.update(invoiced_with_tax=q2(total), invoice_count=int(inv_n), credit_note_count=int(cn_n))
    return {k: FinanceRow(**v) for k, v in acc.items()}


def total(rows: dict[FinanceKey, FinanceRow] | Sequence[FinanceRow]) -> FinanceRow:
    values = rows.values() if isinstance(rows, dict) else rows
    sums: dict[str, Any] = dict.fromkeys(FIN_FIELDS, ZERO)
    sums["invoice_count"] = sums["credit_note_count"] = 0
    for r in values:
        for f in FIN_FIELDS:
            sums[f] = sums[f] + getattr(r, f)
    return FinanceRow(**sums)


async def collections(db: AsyncSession, le_ids: Sequence[uuid.UUID], start: date, end: date) -> Decimal:
    p = T.payments.c
    received = select(func.coalesce(func.sum(p.inr_amount), 0)).where(
        *_doc_filter(p.legal_entity_id, p.payment_date, le_ids, start, end)
    )
    reversed_ = select(func.coalesce(func.sum(p.inr_amount), 0)).where(
        p.allocation_status == "reversed",
        *_doc_filter(p.legal_entity_id, p.reversal_date, le_ids, start, end),
    )
    got = Decimal((await db.execute(received)).scalar_one())
    back = Decimal((await db.execute(reversed_)).scalar_one())
    return q2(got - back)


async def unallocated(db: AsyncSession, le_ids: Sequence[uuid.UUID], as_of: date) -> Decimal:
    cc = T.client_credits.c
    stmt = select(func.coalesce(func.sum(cc.amount), 0)).where(
        cc.payment_id.is_not(None), *_doc_filter(cc.legal_entity_id, cc.credit_date, le_ids, None, as_of)
    )
    return q2((await db.execute(stmt)).scalar_one())


# ================================================================ ops figures
@dataclasses.dataclass(frozen=True, slots=True)
class OpsRow:
    workers_on_billing: int = 0
    placements_started: int = 0
    placements_ended: int = 0
    early_terminations: int = 0
    positions_closed: int = 0
    positions_filled: int = 0

    @property
    def fill_rate(self) -> Decimal | None:
        if not self.positions_closed:
            return None
        return (Decimal(self.positions_filled) / Decimal(self.positions_closed) * 100).quantize(
            CENT, ROUND_HALF_UP
        )


OPS_FIELDS = (
    "workers_on_billing", "placements_started", "placements_ended", "early_terminations",
    "positions_closed", "positions_filled",
)  # fmt: skip
EARLY_TERMINATION_DAYS = 30


async def ops_by_month(
    db: AsyncSession, avail: frozenset[str], le_ids: Sequence[uuid.UUID], start: date, end: date
) -> dict[tuple[uuid.UUID, date], OpsRow]:
    """Live ops counts per (LE, month). Workers on billing need billing; the rest need placements."""
    acc: dict[tuple[uuid.UUID, date], dict[str, int]] = {}
    p = T.placements.c
    if {"placements", *T.BILLING} <= avail:
        lf = line_facts(le_ids, start, end)
        month = cast(func.date_trunc("month", lf.c.doc_date), Date)
        rows = await db.execute(
            select(p.legal_entity_id, month, func.count(distinct(p.candidate_id)))
            .select_from(lf.join(T.placements, p.id == lf.c.placement_id))
            .where(lf.c.src != "credit", lf.c.legal_entity_id == p.legal_entity_id)
            .group_by(p.legal_entity_id, month)
        )
        for le, m, n in rows.tuples():
            acc.setdefault((le, m), {})["workers_on_billing"] = int(n)
    if "placements" in avail:
        for field, col in (("placements_started", p.actual_start_date), ("placements_ended", p.end_date)):
            month = cast(func.date_trunc("month", col), Date)
            stmt = (
                select(p.legal_entity_id, month, func.count())
                .where(p.status != "dropped", *_doc_filter(p.legal_entity_id, col, le_ids, start, end))
                .group_by(p.legal_entity_id, month)
            )
            if field == "placements_ended":
                early = p.end_date <= p.actual_start_date + EARLY_TERMINATION_DAYS
                stmt = stmt.add_columns(func.count().filter(early))
            for row in (await db.execute(stmt)).tuples():
                bucket = acc.setdefault((row[0], row[1]), {})
                bucket[field] = int(row[2])
                if field == "placements_ended":
                    bucket["early_terminations"] = int(row[3])
        # Fill rate [ASSUMPTION, PRD open item 11]: Demands that reached filled / closed / cancelled in the
        # month (status + last update), attributed to the LE of their Placements.
        from app.models.demands import Demand

        dmonth = cast(func.date_trunc("month", Demand.updated_at), Date)
        demand_le = (
            select(distinct(p.demand_id).label("demand_id"), p.legal_entity_id)
            .where(
                p.demand_id.is_not(None),
                *(_doc_filter(p.legal_entity_id, p.legal_entity_id, le_ids, None, None)),
            )
            .subquery()
        )
        rows = await db.execute(
            select(
                demand_le.c.legal_entity_id,
                dmonth,
                func.sum(Demand.positions),
                func.sum(Demand.positions_filled),
            )
            .select_from(Demand)
            .join(demand_le, demand_le.c.demand_id == Demand.id)
            .where(
                Demand.status.in_(("filled", "closed", "cancelled")),
                cast(Demand.updated_at, Date) >= start,
                cast(Demand.updated_at, Date) <= end,
            )
            .group_by(demand_le.c.legal_entity_id, dmonth)
        )
        for le, m, positions, filled in rows.tuples():
            bucket = acc.setdefault((le, m), {})
            bucket["positions_closed"] = int(positions or 0)
            bucket["positions_filled"] = int(filled or 0)
    return {k: OpsRow(**v) for k, v in acc.items()}


async def pipeline_value(db: AsyncSession) -> Decimal:
    """Σ weighted value (estimated revenue × probability) of open Opportunities, today (CRM projection)."""
    o = T.opportunities.c
    weighted = func.sum(o.estimated_revenue * o.probability_pct / 100)
    stmt = select(func.coalesce(weighted, 0)).where(
        o.archived_at.is_(None), o.stage.not_in(("won", "lost", "closed_won", "closed_lost"))
    )
    return q2((await db.execute(stmt)).scalar_one())


def numeric(value: Any) -> Any:
    return cast(value, Numeric(16, 2))


__all__ = [
    "FinanceRow",
    "OpsRow",
    "collections",
    "finance_by_month",
    "header_facts",
    "line_facts",
    "margin_pct",
    "money",
    "months_between",
    "ops_by_month",
    "pipeline_value",
    "q2",
    "total",
    "unallocated",
]
