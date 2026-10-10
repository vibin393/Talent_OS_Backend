# ruff: noqa: E501, N812
"""Rebuildable monthly summaries, reconciliation and the executive grid (Epic 34, A2/A3, AD-33, FR-177).

* ``rebuild(db, legal_entity_id, month)`` -- delete + re-insert one (LE, month) key of
  ``reporting_finance_monthly`` / ``reporting_ops_monthly`` from ``facts`` (idempotent, one transaction).
* ``verify(db, months)`` -- recompute every (LE, month) live and compare with ``Decimal`` equality; a mismatch
  records a ``reporting_reconciliation_issues`` row and rebuilds the key; a mismatch that survives the rebuild
  alerts once (``reporting_reconciliation_failed`` to Finance Admins + a Platform Operator log alert).
* ``exec_grid(...)`` -- months x metrics for one LE; a key with an open issue is served live (``source: live``).

Open balance and DSO come only from ``receivables_service`` (``aging_query`` / ``dso``) through ``ar``; until
Epic 30 implements them those cells are ``null`` (not available).
"""

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.reporting import ReportingFinanceMonthly, ReportingOpsMonthly, ReportingReconciliationIssue
from app.services.reporting import facts
from app.services.reporting import p2tables as T
from app.services.reporting.common import available, now, tenant_of

RF, RO, RI = ReportingFinanceMonthly, ReportingOpsMonthly, ReportingReconciliationIssue
NOTIFICATION_KEY = "reporting_reconciliation_failed"


def has_billing(avail: frozenset[str]) -> bool:
    return set(T.BILLING) <= avail


# ================================================================ AR bridge (Epic 30 owner functions)
class NotAvailable(Exception):  # noqa: N818 - a state, not an error
    """An owner function / table this figure needs is not there yet: the figure is 'not available'."""


def _receivables_fn(name: str) -> Any:
    from app.services import receivables_service

    fn = getattr(receivables_service, name, None)
    if fn is None:
        raise NotAvailable(name)
    return fn


async def aging(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, as_of: date) -> Any:
    """``receivables_service.aging_query`` (group by entity) -- the only open-balance implementation."""
    try:
        return await _receivables_fn("aging_query")(ctx, db, legal_entity_id, as_of, "entity")
    except NotImplementedError as exc:
        raise NotAvailable("aging_query") from exc


async def dso(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date
) -> Decimal | None:
    try:
        value = await _receivables_fn("dso")(ctx, db, legal_entity_id, start, end)
    except NotImplementedError as exc:
        raise NotAvailable("dso") from exc
    return None if value is None else Decimal(str(value))


def bucket_rows(result: Any) -> list[dict[str, Any]]:
    """Return an ``AgingOut`` as ``[{bucket, amount: Decimal (INR), disputed: Decimal}]`` in bucket order.

    Disputed amounts are reported by the receivables domain per row (document currency); only INR rows are
    summed here so no currency is mixed (the disputed series is informational)."""
    labels = list(getattr(result, "bucket_labels", None) or [])
    totals = dict(getattr(result, "totals_inr", None) or {})
    disputed = sum(
        (
            Decimal(str(r.disputed))
            for r in getattr(result, "rows", []) or []
            if getattr(r, "currency", "INR") == "INR"
        ),
        Decimal(0),
    )
    out = [{"bucket": b, "amount": Decimal(str(totals.get(b, 0))), "disputed": Decimal(0)} for b in labels]
    if out:
        out.append({"bucket": "disputed", "amount": Decimal(0), "disputed": disputed})
    return out


async def overdue_ar(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, as_of: date
) -> Decimal:
    """Σ aging buckets after *Not Due* (receivables' single open-balance definition), INR."""
    rows = bucket_rows(await aging(ctx, db, legal_entity_id, as_of))
    return facts.q2(
        sum((r["amount"] for r in rows if r["bucket"] not in ("not_due", "disputed")), Decimal(0))
    )


# ================================================================ rebuild
async def legal_entity_ids(db: AsyncSession) -> list[uuid.UUID]:
    if "legal_entities" not in await available(db):
        return []
    return list((await db.execute(select(T.legal_entities.c.id))).scalars())


async def rebuild(db: AsyncSession, legal_entity_id: uuid.UUID, month: date) -> tuple[int, int]:
    """Delete + re-insert one (LE, month) key from the live facts. Returns (finance rows, ops rows)."""
    month = facts.month_start(month)
    tenant_id = tenant_of(db)
    avail = await available(db)
    end = facts.month_end(month)
    await db.execute(delete(RF).where(RF.legal_entity_id == legal_entity_id, RF.month == month))
    await db.execute(delete(RO).where(RO.legal_entity_id == legal_entity_id, RO.month == month))
    fin_n = ops_n = 0
    if has_billing(avail):
        for (le, m, client), row in (await facts.finance_by_month(db, [legal_entity_id], month, end)).items():
            db.add(
                RF(
                    tenant_id=tenant_id, legal_entity_id=le, month=m, client_id=client,
                    revenue_inr=row.revenue, invoiced_with_tax_inr=row.invoiced_with_tax, cost_inr=row.cost,
                    credit_inr=row.credit, debit_inr=row.debit, invoice_count=row.invoice_count,
                    credit_note_count=row.credit_note_count, computed_at=now(),
                )
            )  # fmt: skip
            fin_n += 1
    for (le, m), ops in (await facts.ops_by_month(db, avail, [legal_entity_id], month, end)).items():
        db.add(RO(tenant_id=tenant_id, legal_entity_id=le, month=m, computed_at=now(), **_ops_values(ops)))
        ops_n += 1
    await db.flush()
    return fin_n, ops_n


def _ops_values(ops: facts.OpsRow) -> dict[str, int]:
    return {f: getattr(ops, f) for f in facts.OPS_FIELDS}


def recent_months(today: date, count: int) -> list[date]:
    last = facts.month_start(today)
    return [facts.add_months(last, -i) for i in range(count - 1, -1, -1)]


async def rebuild_recent(db: AsyncSession, months: int = 3, today: date | None = None) -> int:
    n = 0
    for le in await legal_entity_ids(db):
        for m in recent_months(today or now().date(), months):
            await rebuild(db, le, m)
            n += 1
    return n


# ================================================================ reconciliation
async def summary_finance(
    db: AsyncSession, legal_entity_id: uuid.UUID, month: date
) -> dict[uuid.UUID, facts.FinanceRow]:
    rows = (
        await db.execute(select(RF).where(RF.legal_entity_id == legal_entity_id, RF.month == month))
    ).scalars()
    return {
        r.client_id: facts.FinanceRow(
            revenue=facts.q2(r.revenue_inr), invoiced_with_tax=facts.q2(r.invoiced_with_tax_inr),
            cost=facts.q2(r.cost_inr), credit=facts.q2(r.credit_inr), debit=facts.q2(r.debit_inr),
            invoice_count=r.invoice_count, credit_note_count=r.credit_note_count,
        )
        for r in rows
    }  # fmt: skip


async def _differences(
    db: AsyncSession, legal_entity_id: uuid.UUID, month: date
) -> list[tuple[str, str, str]]:
    """Figures where summary != live for one key: ``[(figure, summary, live)]`` (totals over clients)."""
    live = await facts.finance_by_month(db, [legal_entity_id], month, facts.month_end(month))
    live_by_client = {k[2]: v for k, v in live.items() if k[1] == month}
    stored = await summary_finance(db, legal_entity_id, month)
    out: list[tuple[str, str, str]] = []
    for client in set(live_by_client) | set(stored):
        a, b = stored.get(client, facts.FinanceRow()), live_by_client.get(client, facts.FinanceRow())
        for f in facts.FIN_FIELDS:
            if getattr(a, f) != getattr(b, f):
                out.append((f"{f}:{client}", str(getattr(a, f)), str(getattr(b, f))))
    return out


async def verify(
    db: AsyncSession, months: Sequence[date] | None = None, today: date | None = None
) -> dict[str, int]:
    """Compare every (LE, month) summary with the live facts (exact ``Decimal`` equality)."""
    from app.core.settings import get_settings

    if not has_billing(await available(db)):
        return {"checked": 0, "issues": 0, "persistent": 0}
    months = list(months or recent_months(today or now().date(), get_settings().reporting_summary_months))
    checked = issues = persistent = 0
    for le in await legal_entity_ids(db):
        for month in months:
            checked += 1
            diffs = await _differences(db, le, month)
            if not diffs:
                await db.execute(
                    update(RI)
                    .where(RI.legal_entity_id == le, RI.month == month, RI.resolved_at.is_(None))
                    .values(resolved_at=now())
                )
                continue
            issues += 1
            records = [
                RI(tenant_id=tenant_of(db), legal_entity_id=le, month=month, figure=f[:40], summary_value=s[:40], live_value=v[:40])
                for f, s, v in diffs
            ]  # fmt: skip
            db.add_all(records)
            await db.flush()
            await rebuild(db, le, month)
            if await _differences(db, le, month):
                persistent += 1
                await _alert(db, le, month, records)
            else:
                for r in records:
                    r.resolved_at = now()
            await db.flush()
    return {"checked": checked, "issues": issues, "persistent": persistent}


async def _alert(
    db: AsyncSession, le: uuid.UUID, month: date, records: list[ReportingReconciliationIssue]
) -> None:
    """Alert once per key on a persistent mismatch (Platform Operator log alert + Finance Admin notification)."""
    already = (
        await db.execute(
            select(func.count()).where(RI.legal_entity_id == le, RI.month == month, RI.alerted_at.is_not(None), RI.resolved_at.is_(None))
        )
    ).scalar_one()  # fmt: skip
    stamp = now()
    for r in records:
        r.alerted_at = stamp
    if already:
        return
    logger.error(
        "PLATFORM ALERT reporting reconciliation mismatch tenant={} le={} month={}", tenant_of(db), le, month
    )
    try:
        from app.services import notifications_service as notifications
        from app.services.reporting.common import role_user_ids

        recipients = await role_user_ids(db, "finance_admin")
        if recipients and NOTIFICATION_KEY in notifications.NOTIFICATION_TYPES:
            await notifications.deliver(
                db,
                NOTIFICATION_KEY,
                recipients,
                notifications.Content(
                    title="Reporting figures need attention",
                    body=f"Summary figures for {month:%b %Y} differ from the issued documents after a rebuild.",
                ),
                event_id=f"rpt-recon:{le}:{month.isoformat()}",
            )
    except Exception as exc:  # the alert must never break the nightly job
        logger.warning("Reconciliation alert could not be delivered ({})", type(exc).__name__)


async def open_issue_keys(db: AsyncSession) -> set[tuple[uuid.UUID, date]]:
    rows = await db.execute(select(RI.legal_entity_id, RI.month).where(RI.resolved_at.is_(None)).distinct())
    return set(rows.tuples().all())


async def open_issues(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(RI).where(RI.resolved_at.is_(None)).order_by(RI.detected_at.desc()).limit(500)
        )
    ).scalars()
    return [
        {
            "id": str(r.id), "legal_entity_id": str(r.legal_entity_id), "month": r.month.isoformat(),
            "figure": r.figure, "summary": r.summary_value, "live": r.live_value,
            "detected_at": r.detected_at.isoformat(), "alerted": r.alerted_at is not None,
        }
        for r in rows
    ]  # fmt: skip


# ================================================================ executive grid
GRID_METRICS: tuple[tuple[str, str, str], ...] = (
    ("revenue", "Revenue (ex-GST)", "money"),
    ("margin", "Gross margin", "money"),
    ("margin_pct", "Margin %", "percent"),
    ("workers_on_billing", "Workers on billing", "count"),
    ("placements_started", "New placements", "count"),
    ("placements_ended", "Placements ended", "count"),
    ("fill_rate", "Fill rate %", "percent"),
    ("pipeline", "Sales pipeline (weighted)", "money"),
    ("dso", "DSO (days)", "number"),
    ("overdue_ar", "Overdue AR", "money"),
)
COMPENSATION_METRICS = frozenset({"margin", "margin_pct"})


async def _summary_month_totals(
    db: AsyncSession, le: uuid.UUID, first: date, last: date
) -> tuple[dict[date, facts.FinanceRow], dict[date, facts.OpsRow]]:
    fin: dict[date, list[facts.FinanceRow]] = {}
    for r in (
        await db.execute(select(RF).where(RF.legal_entity_id == le, RF.month >= first, RF.month <= last))
    ).scalars():
        fin.setdefault(r.month, []).append(
            facts.FinanceRow(
                revenue=facts.q2(r.revenue_inr), invoiced_with_tax=facts.q2(r.invoiced_with_tax_inr),
                cost=facts.q2(r.cost_inr), credit=facts.q2(r.credit_inr), debit=facts.q2(r.debit_inr),
                invoice_count=r.invoice_count, credit_note_count=r.credit_note_count,
            )
        )  # fmt: skip
    ops = {
        r.month: facts.OpsRow(**{f: getattr(r, f) for f in facts.OPS_FIELDS})
        for r in (
            await db.execute(select(RO).where(RO.legal_entity_id == le, RO.month >= first, RO.month <= last))
        ).scalars()
    }
    return {m: facts.total(v) for m, v in fin.items()}, ops


async def exec_grid(
    ctx: RequestContext,
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    first: date,
    last: date,
    *,
    compensation: bool,
) -> dict[str, Any]:
    avail = await available(db)
    months = facts.months_between(first, last)
    fin, ops = await _summary_month_totals(db, legal_entity_id, first, last)
    issues = await open_issue_keys(db)
    pipeline: Decimal | None = None
    if "opportunities" in avail:
        pipeline = await facts.pipeline_value(db)
    today = now().date()
    cells: dict[str, list[dict[str, Any]]] = {key: [] for key, _l, _k in GRID_METRICS}
    for m in months:
        source = "summary"
        f_row, o_row = fin.get(m, facts.FinanceRow()), ops.get(m, facts.OpsRow())
        if (legal_entity_id, m) in issues or m == facts.month_start(today):
            # Open reconciliation issue (or the current, still-moving month): serve the live facts.
            source = "live" if (legal_entity_id, m) in issues else "summary"
            if (legal_entity_id, m) in issues and has_billing(avail):
                f_row = facts.total(
                    await facts.finance_by_month(db, [legal_entity_id], m, facts.month_end(m))
                )
                o_row = (await facts.ops_by_month(db, avail, [legal_entity_id], m, facts.month_end(m))).get(
                    (legal_entity_id, m), facts.OpsRow()
                )
        billing = has_billing(avail)
        values: dict[str, Any] = {
            "revenue": facts.money(f_row.revenue) if billing else None,
            "margin": facts.money(f_row.margin) if billing else None,
            "margin_pct": (str(f_row.margin_pct) if f_row.margin_pct is not None else None)
            if billing
            else None,
            "workers_on_billing": o_row.workers_on_billing if billing else None,
            "placements_started": o_row.placements_started,
            "placements_ended": o_row.placements_ended,
            "fill_rate": str(o_row.fill_rate) if o_row.fill_rate is not None else None,
            "pipeline": facts.money(pipeline)
            if pipeline is not None and m == facts.month_start(today)
            else None,
        }
        try:
            d = await dso(ctx, db, legal_entity_id, m, facts.month_end(m))
            values["dso"] = None if d is None else str(d)
        except NotAvailable:
            values["dso"] = None
        try:
            values["overdue_ar"] = facts.money(
                await overdue_ar(ctx, db, legal_entity_id, min(facts.month_end(m), today))
            )
        except NotAvailable:
            values["overdue_ar"] = None
        for key, _label, _kind in GRID_METRICS:
            masked = key in COMPENSATION_METRICS and not compensation
            cells[key].append(
                {"month": m.isoformat(), "value": None if masked else values.get(key), "source": source}
            )
    rows = [
        {
            "key": key,
            "label": label,
            "kind": kind,
            "masked": key in COMPENSATION_METRICS and not compensation,
            "cells": cells[key],
        }
        for key, label, kind in GRID_METRICS
    ]
    return {
        "legal_entity_id": str(legal_entity_id),
        "months": [m.isoformat() for m in months],
        "rows": rows,
        "computed_at": now().isoformat(),
    }


def month_of(value: date | datetime) -> date:
    return (value.date() if isinstance(value, datetime) else value).replace(day=1)
