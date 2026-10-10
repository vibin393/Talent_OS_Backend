"""Executive dashboard tiles (Epic 34, A3): read the monthly summaries (``summaries.exec_grid``)."""

from typing import Any

from app.services.reporting import facts, summaries
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import comp, need


async def _grid(t: TileContext) -> dict[str, Any]:
    need(t, "legal_entities")
    les = list(t.filters.legal_entity_ids) or await summaries.legal_entity_ids(t.db)
    if not les:
        raise summaries.NotAvailable("legal_entities")
    last = facts.month_start(t.filters.to_date)
    first = facts.add_months(last, -11)
    return await summaries.exec_grid(t.ctx, t.db, les[0], first, last, compensation=comp(t))


def _source(grid: dict[str, Any]) -> str:
    live = any(c["source"] == "live" for r in grid["rows"] for c in r["cells"])
    return "live" if live else "summary"


async def exec_grid(t: TileContext) -> TileData:
    grid = await _grid(t)
    return {
        "rows": grid["rows"],
        "source": _source(grid),
        "link": {"path": "/reports/executive", "query": {}},
    }


def _series(grid: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for row in grid["rows"]:
        if row["key"] not in keys:
            continue
        points = []
        for c in row["cells"]:
            v = c["value"]
            points.append({"bucket": c["month"], "value": v["amount"] if isinstance(v, dict) else v})
        out.append({"key": row["key"], "label": row["label"], "points": points, "masked": row["masked"]})
    return out


async def revenue_margin_trend(t: TileContext) -> TileData:
    grid = await _grid(t)
    keys = ("revenue", "margin") if comp(t) else ("revenue",)
    return {"series": _series(grid, keys), "source": _source(grid), "unit": "INR", "masked": not comp(t)}


async def ar_overdue_trend(t: TileContext) -> TileData:
    grid = await _grid(t)
    series = _series(grid, ("overdue_ar",))
    if all(p["value"] is None for s in series for p in s["points"]):
        raise summaries.NotAvailable("aging_query")
    return {"series": series, "source": _source(grid), "unit": "INR"}
