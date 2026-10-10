# ruff: noqa: N812
"""Account operations dashboard tiles (Epic 34, A3): AM view, placement scope applies."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.core.permissions import Scope
from app.services.reporting import facts
from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import P, comp, le_link, names, need, scoped_placements

ACTIVE = ("active", "offboarding")


async def active_placements_by_client(t: TileContext) -> TileData:
    need(t, "placements")
    ids = scoped_placements(t, P.status.in_(ACTIVE))
    rows = (
        await t.db.execute(
            select(P.client_id, func.count())
            .where(P.id.in_(ids))
            .group_by(P.client_id)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()
    nm = await names(t, {r[0] for r in rows})
    return {
        "value": sum(int(r[1]) for r in rows),
        "rows": [{"client_id": str(c), "client": nm.get(c), "active": int(n)} for c, n in rows],
        "row_links": [le_link(t, "/placements", status="active", client_id=str(c)) for c, _n in rows],
        "link": le_link(t, "/placements", status="active"),
    }


async def placements_ending_30_60(t: TileContext) -> TileData:
    need(t, "placements")
    today = t.now.date()
    end = P.planned_end_date
    ids = scoped_placements(t, P.status.in_(ACTIVE), end >= today, end <= today + timedelta(days=60))
    n30, n60 = (
        await t.db.execute(
            select(func.count().filter(end <= today + timedelta(days=30)), func.count()).where(P.id.in_(ids))
        )
    ).one()
    return {
        "rows": [
            {"key": "30", "label": "Next 30 days", "value": int(n30)},
            {"key": "60", "label": "Next 60 days", "value": int(n60)},
        ],
        "row_links": [
            le_link(t, "/placements", ending_within_days="30"),
            le_link(t, "/placements", ending_within_days="60"),
        ],
    }


async def extensions_in_progress(t: TileContext) -> TileData:
    need(t, "placements", "placement_extensions")
    e = T.placement_extensions.c
    n = (
        await t.db.execute(
            select(func.count()).where(
                e.status.not_in(("confirmed", "declined", "cancelled", "completed")),
                e.placement_id.in_(scoped_placements(t)),
            )
        )
    ).scalar_one()
    return {"value": int(n)}


async def po_near_limit(t: TileContext) -> TileData:
    need(t, "purchase_orders", "po_consumptions")
    po, pc = T.purchase_orders.c, T.po_consumptions.c
    consumed = select(func.coalesce(func.sum(pc.amount), 0)).where(pc.po_id == po.id).scalar_subquery()
    today = t.now.date()
    stmt = (
        select(
            po.id, po.po_number, po.client_id, po.value_cap, consumed.label("used"), po.valid_to, po.currency
        )
        .where(po.status == "active")
        .where(
            ((po.value_cap.is_not(None)) & ((po.value_cap - consumed) < po.value_cap * Decimal("0.15")))
            | (po.valid_to <= today + timedelta(days=30))
        )
        .order_by(po.valid_to)
        .limit(20)
    )
    if t.filters.legal_entity_ids:
        stmt = stmt.where(po.legal_entity_id.in_(t.filters.legal_entity_ids))
    rows = (await t.db.execute(stmt)).all()
    nm = await names(t, {r.client_id for r in rows})
    out: list[dict[str, Any]] = []
    for r in rows:
        bal = None if r.value_cap is None else facts.money(Decimal(r.value_cap) - Decimal(r.used), r.currency)
        out.append(
            {
                "po": r.po_number,
                "client": nm.get(r.client_id),
                "balance": bal,
                "valid_to": r.valid_to.isoformat(),
            }
        )
    return {"value": len(out), "rows": out, "link": le_link(t, "/purchase-orders", near_limit="true")}


async def timesheets_pending_client(t: TileContext) -> TileData:
    need(t, "timesheets", "placements")
    ts = T.timesheets.c
    n = (
        await t.db.execute(
            select(func.count()).where(ts.status == "submitted", ts.placement_id.in_(scoped_placements(t)))
        )
    ).scalar_one()
    return {"value": int(n), "link": le_link(t, "/timesheets", status="submitted")}


async def revenue_margin_by_client_month(t: TileContext) -> TileData:
    need(t, *T.BILLING)
    f = t.filters
    rows = await facts.finance_by_month(t.db, f.legal_entity_ids, f.from_date, f.to_date)
    # Account Managers with own/team placement scope: only Clients of their placements.
    visible = None
    scope_ids = scoped_placements(t)
    scope = t.principal.scope_of("placement:read")
    if scope is None or not scope.covers(Scope.TENANT):
        visible = set(
            (await t.db.execute(select(P.client_id).where(P.id.in_(scope_ids)).distinct())).scalars()
        )
    by: dict[tuple[Any, Any], list[facts.FinanceRow]] = {}
    for (_le, month, client), r in rows.items():
        if visible is not None and client not in visible:
            continue
        by.setdefault((client, month), []).append(r)
    nm = await names(t, {k[0] for k in by})
    show = comp(t)
    out = []
    for (client, month), parts in sorted(by.items(), key=lambda kv: (kv[0][1], str(nm.get(kv[0][0])))):
        tot = facts.total(parts)
        row: dict[str, Any] = {
            "client": nm.get(client),
            "month": month.isoformat(),
            "revenue": facts.money(tot.revenue),
        }
        if show:
            row["margin"] = facts.money(tot.margin)
            row["margin_pct"] = None if tot.margin_pct is None else str(tot.margin_pct)
        out.append(row)
    return {"rows": out[:50], "masked": not show}
