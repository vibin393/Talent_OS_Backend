# ruff: file-ignore[line-too-long]
"""AR aging and DSO (FR-155, AD-33): pure queries over facts dated <= ``as_of`` -- aging of any past date is
reproducible. Buckets by ``as_of - due_date`` with the tenant buckets (plus Not Due); totals in document currency
and INR (open INR at the document ``fx_rate``); unallocated credits and disputed amounts as separate columns."""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.billing import CreditNote
from app.models.receivables import ClientCredit
from app.schemas.receivables import AgingOut, AgingRow, DsoOut
from app.services.receivables import settlement
from app.services.receivables.common import ZERO, documents, q2
from app.services.receivables.settings import buckets_of, get_settings_row

GroupBy = Literal["entity", "client", "document"]


def bucket_labels(limits: list[int]) -> list[str]:
    labels = ["not_due"]
    lo = 1
    for hi in limits:
        labels.append(f"{lo}-{hi}")
        lo = hi + 1
    labels.append(f"{lo}+")
    return labels


def bucket_for(days: int, limits: list[int]) -> str:
    labels = bucket_labels(limits)
    if days <= 0:
        return labels[0]
    for i, hi in enumerate(limits):
        if days <= hi:
            return labels[i + 1]
    return labels[-1]


async def _credits(db: AsyncSession, legal_entity_id: uuid.UUID, as_of: date, client_id: uuid.UUID | None) -> dict[tuple[uuid.UUID, str], Decimal]:
    stmt = (
        select(ClientCredit.client_id, ClientCredit.currency, func.sum(ClientCredit.amount))
        .where(ClientCredit.legal_entity_id == legal_entity_id, ClientCredit.credit_date <= as_of)
        .group_by(ClientCredit.client_id, ClientCredit.currency)
    )
    if client_id is not None:
        stmt = stmt.where(ClientCredit.client_id == client_id)
    return {(r[0], r[1]): Decimal(r[2]) for r in (await db.execute(stmt)).all()}


async def _client_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    from app.models.clients import Client

    return {r[0]: r[1] for r in (await db.execute(select(Client.id, Client.name).where(Client.id.in_(ids)))).all()}


async def query(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    as_of: date,
    group_by: GroupBy = "client",
    client_id: uuid.UUID | None = None,
) -> AgingOut:
    limits = buckets_of(await get_settings_row(db))
    labels = bucket_labels(limits)
    docs = await documents(db, legal_entity_id=legal_entity_id, client_id=client_id, as_of=as_of)
    bals = await settlement.balances(db, docs, as_of)
    credits = await _credits(db, legal_entity_id, as_of, client_id)
    names = await _client_names(db, {d.client_id for d in docs} | {k[0] for k in credits})
    rows: dict[tuple[str, str], AgingRow] = {}
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)

    def row_for(key: str, label: str, currency: str, **extra: object) -> AgingRow:
        k = (key, currency)
        if k not in rows:
            rows[k] = AgingRow(
                key=key, label=label, currency=currency, buckets=dict.fromkeys(labels, ZERO), total_open=ZERO, total_open_inr=ZERO,
                disputed=ZERO, unallocated_credit=ZERO, **extra,  # type: ignore[arg-type]
            )  # fmt: skip
        return rows[k]

    for b in bals.values():
        if b.open <= 0:
            continue
        d = b.doc
        due = d.due_date or d.doc_date or as_of
        bucket = bucket_for((as_of - due).days, limits)
        open_inr = q2(b.open * d.fx)
        if group_by == "document":
            r = row_for(str(d.id), d.number or str(d.id), d.currency, client_id=d.client_id, document_kind=d.kind, document_id=d.id, due_date=d.due_date)
        elif group_by == "client":
            r = row_for(str(d.client_id), names.get(d.client_id, str(d.client_id)), d.currency, client_id=d.client_id)
        else:
            r = row_for(str(legal_entity_id), "Legal entity", d.currency)
        r.buckets[bucket] = q2(r.buckets[bucket] + b.open)
        r.total_open = q2(r.total_open + b.open)
        r.total_open_inr = q2(r.total_open_inr + open_inr)
        r.disputed = q2(r.disputed + min(b.disputed, b.open))
        totals[bucket] = q2(totals[bucket] + open_inr)
        totals["total"] = q2(totals["total"] + open_inr)
    if group_by != "document":
        for (cid, ccy), amount in credits.items():
            if amount == 0:
                continue
            r = row_for(str(cid), names.get(cid, str(cid)), ccy, client_id=cid) if group_by == "client" else row_for(str(legal_entity_id), "Legal entity", ccy)
            r.unallocated_credit = q2(r.unallocated_credit + amount)
            if ccy == "INR":
                totals["unallocated_credit"] = q2(totals["unallocated_credit"] + amount)
    return AgingOut(
        as_of=as_of, legal_entity_id=legal_entity_id, group_by=group_by, bucket_labels=labels,
        rows=sorted(rows.values(), key=lambda r: (-r.total_open_inr, r.label)), totals_inr=dict(totals),
    )  # fmt: skip


async def ar_open_inr(db: AsyncSession, legal_entity_id: uuid.UUID, as_of: date) -> Decimal:
    docs = await documents(db, legal_entity_id=legal_entity_id, as_of=as_of)
    bals = await settlement.balances(db, docs, as_of, with_disputes=False)
    return q2(sum((q2(b.open * b.doc.fx) for b in bals.values() if b.open > 0), ZERO))


async def dso(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, period_start: date, period_end: date) -> DsoOut:  # ruff: ignore[unused-function-argument]
    ar = await ar_open_inr(db, legal_entity_id, period_end)
    docs = [d for d in await documents(db, legal_entity_id=legal_entity_id, as_of=period_end) if d.doc_date and d.doc_date >= period_start and not d.cancelled_by(period_end)]
    sales = sum((d.inr_document_total for d in docs), ZERO)
    cn = (
        await db.execute(
            select(func.coalesce(func.sum(CreditNote.inr_document_total), 0)).where(
                CreditNote.legal_entity_id == legal_entity_id,
                CreditNote.status == "issued",
                CreditNote.doc_date >= period_start,
                CreditNote.doc_date <= period_end,
            )
        )
    ).scalar()
    net = q2(sales - Decimal(cn or 0))
    days = (period_end - period_start).days + 1
    value = None if net == 0 else q2(ar / net * days)
    return DsoOut(legal_entity_id=legal_entity_id, period_start=period_start, period_end=period_end, dso=value, ar_open_inr=ar, net_sales_inr=net)
