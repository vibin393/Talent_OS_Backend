# ruff: noqa: E501
"""Internal mobility dashboard tiles (Epic 52 A4; Epic 41 data, ``mobility:manage``)."""

from sqlalchemy import func, select

from app.models.mobility import InternalTransfer, MobilityApplication
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p3common import label, need


async def internal_fill_rate(t: TileContext) -> TileData:
    """SM-24 through the owner's ``mobility_service.internal_fill_rate`` (internal transfers / all hires in range)."""
    need(t, "internal_transfers")
    from app.services import mobility_service

    out = await mobility_service.internal_fill_rate(t.ctx, t.db, t.filters.from_date, t.filters.to_date)
    return {"value": out.rate_pct, "unit": "%", "n": out.filled_total, "rows": [
        {"key": "filled_internal", "label": "Filled internally", "value": out.filled_internal},
        {"key": "filled_total", "label": "Filled", "value": out.filled_total},
    ]}  # fmt: skip


async def internal_applications_by_status(t: TileContext) -> TileData:
    need(t, "mobility_applications")
    f = t.filters
    m = MobilityApplication
    counts = dict(
        (
            await t.db.execute(
                select(m.status, func.count())
                .where(m.submitted_at >= f.start, m.submitted_at < f.end)
                .group_by(m.status)
            )
        )
        .tuples()
        .all()
    )
    rows = [{"key": k, "label": label(k), "value": int(v)} for k, v in sorted(counts.items())]
    return {
        "value": sum(r["value"] for r in rows),
        "rows": rows,
        "link": {"path": "/internal-jobs", "query": {}},
    }


async def transfers_confirmed(t: TileContext) -> TileData:
    need(t, "internal_transfers")
    f = t.filters
    month = func.date_trunc("month", InternalTransfer.confirmed_at)
    rows = (
        await t.db.execute(
            select(month, func.count())
            .where(InternalTransfer.confirmed_at >= f.start, InternalTransfer.confirmed_at < f.end)
            .group_by(month)
            .order_by(month)
        )
    ).all()
    points = [{"bucket": b.date().isoformat(), "value": int(n)} for b, n in rows]
    return {
        "value": sum(p["value"] for p in points),
        "series": [{"key": "transfers", "label": "Transfers confirmed", "points": points}],
    }
