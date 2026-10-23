# ruff: file-ignore[line-too-long, lowercase-imported-as-non-lowercase, ambiguous-unicode-character-string, invalid-formatter-suppression-comment]
"""Finance dashboard tiles (Epic 34, A3). Figures come only from posted facts (``facts``) and the receivables
owner functions; WIP is the only estimate and is labelled as such."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.services.reporting import facts, summaries
from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import client_scope, comp, le_cond, le_link, names, need

I = T.invoices.c  # ruff: ignore[ambiguous-variable-name]


async def _monitor_rows(t: TileContext, *, status: str | None = None) -> list[Any]:
    from app.services import time_service

    try:
        out = await time_service.monitor(t.ctx, t.db, status=status)
    except NotImplementedError as exc:
        raise summaries.NotAvailable("time.monitor") from exc
    rows = list(out.rows)
    if t.filters.legal_entity_ids:
        rows = [r for r in rows if r.legal_entity_id in t.filters.legal_entity_ids]
    return rows


async def wip_unbilled_by_client(t: TileContext) -> TileData:
    rows = await _monitor_rows(t, status="approved")
    show = comp(t)
    agg: dict[Any, dict[str, Any]] = defaultdict(lambda: {"count": 0, "value": Decimal(0), "known": True})
    for r in rows:
        a = agg[r.client_id, r.client_name, r.currency]
        a["count"] += 1
        if r.estimated_unbilled_value is None:
            a["known"] = False
        else:
            a["value"] += Decimal(str(r.estimated_unbilled_value))
    out = []
    for (cid, cname, cur), a in sorted(agg.items(), key=lambda kv: -kv[1]["value"]):
        row: dict[str, Any] = {"client_id": str(cid), "client": cname, "timesheets": a["count"]}
        if show:
            row["value"] = facts.money(a["value"], cur) if a["known"] else None
        out.append(row)
    return {
        "value": len(rows),
        "rows": out[:20],
        "estimate": True,
        "masked": not show,
        "link": le_link(t, "/timesheets", status="approved"),
    }


async def invoices_issued(t: TileContext) -> TileData:
    need(t, *T.BILLING)
    f = t.filters
    stmt = select(func.count(), func.coalesce(func.sum(I.inr_taxable_total), 0)).where(
        I.status.in_(("issued", "cancelled")),
        (I.status == "issued") | (I.cancel_basis == "full_credit_note"),
        I.is_imported.is_(False),
        I.doc_date >= f.from_date,
        I.doc_date <= f.to_date,
        *le_cond(t, I.legal_entity_id),
    )
    cond = client_scope(t, I.client_id)
    if cond is not None:
        stmt = stmt.where(cond)
    n, amount = (await t.db.execute(stmt)).one()
    return {
        "value": int(n),
        "money": facts.money(amount),
        "link": le_link(
            t, "/billing", status="issued", **{"from": f.from_date.isoformat(), "to": f.to_date.isoformat()}
        ),
    }


async def invoices_pending_irn(t: TileContext) -> TileData:
    need(t, "invoices")
    stmt = select(func.count(), func.min(I.issued_at), func.count().filter(I.irn_report_by <= t.now.date() + timedelta(days=7))).where(
        I.einvoice_status == "pending_irn", *le_cond(t, I.legal_entity_id)
    )  # fmt: skip
    n, oldest, due_soon = (await t.db.execute(stmt)).one()
    age = (t.now - oldest).days if oldest is not None else None
    return {
        "value": int(n),
        "rows": [
            {"key": "oldest_age_days", "label": "Oldest (days)", "value": age},
            {
                "key": "report_by_7d",
                "label": "IRN due within 7 days",
                "value": int(due_soon),
                "highlight": bool(due_soon),
            },
        ],
        "unit": "invoices",
        "link": le_link(t, "/billing", einvoice_status="pending_irn"),
    }


async def ar_aging(t: TileContext) -> TileData:
    les = list(t.filters.legal_entity_ids) or await summaries.legal_entity_ids(t.db)
    totals: dict[str, dict[str, Decimal]] = {}
    order: list[str] = []
    for le in les:
        for r in summaries.bucket_rows(await summaries.aging(t.ctx, t.db, le, t.filters.as_of)):
            if r["bucket"] not in totals:
                order.append(r["bucket"])
                totals[r["bucket"]] = {"amount": Decimal(0), "disputed": Decimal(0)}
            totals[r["bucket"]]["amount"] += r["amount"]
            totals[r["bucket"]]["disputed"] += r["disputed"]
    rows = [
        {"key": b, "label": b.replace("_", " ").capitalize(),
         "value": str(facts.q2(totals[b]["amount"] + totals[b]["disputed"])),
         "money": facts.money(totals[b]["amount"] + totals[b]["disputed"])}
        for b in order
    ]  # fmt: skip
    return {
        "rows": rows,
        "unit": "INR",
        "link": le_link(t, "/receivables/aging", as_of=t.filters.as_of.isoformat()),
    }


async def dso_trend(t: TileContext) -> TileData:
    need(t, "legal_entities")
    les = list(t.filters.legal_entity_ids) or await summaries.legal_entity_ids(t.db)
    last = facts.month_start(t.filters.to_date)
    months = [facts.add_months(last, -i) for i in range(11, -1, -1)]
    series = []
    for le in les:
        points = []
        for m in months:
            d = await summaries.dso(t.ctx, t.db, le, m, facts.month_end(m))
            points.append({"bucket": m.isoformat(), "value": None if d is None else str(d)})
        series.append({"key": str(le), "label": str(le), "points": points})
    return {"series": series, "unit": "days"}


async def collections(t: TileContext) -> TileData:
    need(t, "payments")
    f = t.filters
    amount = await facts.collections(t.db, f.legal_entity_ids, f.from_date, f.to_date)
    return {
        "money": facts.money(amount),
        "link": le_link(
            t, "/reports/payments", **{"from": f.from_date.isoformat(), "to": f.to_date.isoformat()}
        ),
    }


async def promises_overdue(t: TileContext) -> TileData:
    need(t, "collection_activities")
    ca = T.collection_activities.c
    stmt = (
        select(ca.id, ca.client_id, ca.ptp_date, ca.ptp_amount, ca.ptp_currency)
        .where(ca.kind == "promise_to_pay", ca.ptp_status.in_(("open", "broken")), ca.ptp_date < t.now.date())
        .order_by(ca.ptp_date)
        .limit(20)
    )
    cond = client_scope(t, ca.client_id)
    if cond is not None:
        stmt = stmt.where(cond)
    rows = (await t.db.execute(stmt)).all()
    nm = await names(t, {r.client_id for r in rows})
    return {
        "value": len(rows),
        "rows": [
            {
                "client": nm.get(r.client_id),
                "promise_date": r.ptp_date.isoformat(),
                "amount": facts.money(r.ptp_amount, r.ptp_currency or "INR"),
            }
            for r in rows
        ],
        "link": le_link(t, "/receivables/collections", promise="overdue"),
    }


async def disputes_open(t: TileContext) -> TileData:
    need(t, "disputes")
    d = T.disputes.c
    age = func.percentile_cont(0.5).within_group(func.extract("epoch", t.now - d.opened_at) / 86400)
    n, amount, median = (
        await t.db.execute(
            select(func.count(), func.coalesce(func.sum(d.amount), 0), age).where(d.status == "open")
        )
    ).one()
    return {
        "value": int(n),
        "money": facts.money(amount),
        "rows": [
            {
                "key": "median_age_days",
                "label": "Median age (days)",
                "value": None if median is None else round(float(median), 1),
            }
        ],
        "unit": "disputes",
    }


async def unallocated_payments(t: TileContext) -> TileData:
    need(t, "client_credits")
    return {"money": facts.money(await facts.unallocated(t.db, t.filters.legal_entity_ids, t.filters.as_of))}


async def tds_certificates_outstanding(t: TileContext) -> TileData:
    need(t, "tds_deductions")
    td = T.tds_deductions.c
    n, amount = (
        await t.db.execute(
            select(func.count(), func.coalesce(func.sum(td.amount), 0)).where(
                td.certificate_status != "received", *le_cond(t, td.legal_entity_id)
            )
        )
    ).one()
    return {
        "value": int(n),
        "money": facts.money(amount),
        "link": le_link(t, "/receivables/tds", status="expected"),
    }


async def missing_timesheets_value(t: TileContext) -> TileData:
    rows = [r for r in await _monitor_rows(t) if r.missing]
    show = comp(t)
    out = []
    for r in rows[:20]:
        row: dict[str, Any] = {
            "timesheet": r.human_id, "worker": r.worker_name, "client": r.client_name,
            "period_start": r.period_start.isoformat(),
        }  # fmt: skip
        if show:
            v = r.estimated_unbilled_value
            row["value"] = None if v is None else facts.money(v, r.currency)
        out.append(row)
    return {
        "value": len(rows),
        "rows": out,
        "estimate": True,
        "masked": not show,
        "link": le_link(t, "/timesheets", missing="true"),
    }


async def payroll_export_status(t: TileContext) -> TileData:
    need(t, "pay_periods")
    pp = T.pay_periods.c
    latest: Any = None
    if "payroll_export_versions" in t.avail:
        v = T.payroll_export_versions.c
        latest = (
            select(v.pay_period_id, v.kind, v.seq, v.status, v.finalized_at)
            .where(v.kind != "preview")
            .distinct(v.pay_period_id)
            .order_by(v.pay_period_id, v.seq.desc())
            .subquery()
        )
    cols = [pp.id, pp.human_id, pp.period_start, pp.period_end, pp.status]
    stmt = select(*cols)
    if latest is not None:
        stmt = stmt.add_columns(latest.c.kind, latest.c.seq, latest.c.finalized_at).outerjoin(
            latest, latest.c.pay_period_id == pp.id
        )
    rows = (
        await t.db.execute(
            stmt.where(*le_cond(t, pp.legal_entity_id), pp.period_start <= t.filters.to_date)
            .order_by(pp.period_start.desc())
            .limit(12)
        )
    ).all()
    out = []
    for r in rows:
        m = r._mapping
        out.append(
            {
                "period": f"{m['period_start'].isoformat()} – {m['period_end'].isoformat()}",
                "status": m["status"],
                "version": None if m.get("seq") is None else f"{m.get('kind')} #{m.get('seq')}",
                "finalized_at": m["finalized_at"].isoformat() if m.get("finalized_at") else None,
            }
        )
    return {"rows": out, "link": le_link(t, "/payroll")}


def _unused(_: date) -> None:  # keeps the date import for typing helpers
    return None
