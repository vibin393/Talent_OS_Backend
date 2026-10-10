# ruff: noqa: N812, RUF001
"""Compliance dashboard tiles (Epic 34, A3): BGV, credentials and non-compliant placements."""

from datetime import timedelta

from sqlalchemy import case, func, select

from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import P, le_link, need, scoped_placements

BGV_TERMINAL = ("clear", "failed", "cancelled")


async def bgv_by_status_age(t: TileContext) -> TileData:
    need(t, "bgv_cases")
    b = T.bgv_cases.c
    age = func.extract("epoch", t.now - b.created_at) / 86400
    bucket = case((age <= 7, "le_7"), (age <= 14, "d8_14"), else_="gt_14")
    rows = (
        await t.db.execute(
            select(b.status, bucket, func.count())
            .where(b.status.not_in(BGV_TERMINAL))
            .group_by(b.status, bucket)
        )
    ).all()
    by: dict[str, dict[str, int]] = {}
    for status, bk, n in rows:
        by.setdefault(status, {"le_7": 0, "d8_14": 0, "gt_14": 0})[bk] = int(n)
    return {
        "rows": [{"key": s, "label": s.replace("_", " ").capitalize(), **v} for s, v in sorted(by.items())],
        "series_keys": [
            {"key": "le_7", "label": "≤ 7 days"},
            {"key": "d8_14", "label": "8–14 days"},
            {"key": "gt_14", "label": "> 14 days"},
        ],
        "link": le_link(t, "/bgv"),
    }


async def discrepancies_awaiting_response(t: TileContext) -> TileData:
    need(t, "bgv_discrepancies")
    d = T.bgv_discrepancies.c
    n, overdue = (
        await t.db.execute(
            select(func.count(), func.count().filter(d.response_due_on < t.now.date())).where(
                d.status == "awaiting_response"
            )
        )
    ).one()
    return {
        "value": int(n),
        "rows": [{"key": "overdue", "label": "Past response date", "value": int(overdue)}],
        "unit": "discrepancies",
        "link": le_link(t, "/bgv", discrepancy="awaiting_response"),
    }


async def credentials_expiring(t: TileContext) -> TileData:
    need(t, "credentials")
    c = T.credentials.c
    today = t.now.date()
    live = (
        c.superseded_at.is_(None),
        c.erased_at.is_(None),
        c.expires_on >= today,
        c.expires_on <= today + timedelta(days=90),
    )
    n30, n60, n90 = (
        await t.db.execute(
            select(
                func.count().filter(c.expires_on <= today + timedelta(days=30)),
                func.count().filter(c.expires_on <= today + timedelta(days=60)),
                func.count(),
            ).where(*live)
        )
    ).one()
    return {
        "rows": [
            {"key": "30", "label": "30 days", "value": int(n30)},
            {"key": "60", "label": "60 days", "value": int(n60)},
            {"key": "90", "label": "90 days", "value": int(n90)},
        ],
        "row_links": [le_link(t, "/compliance", expiring_within_days=d) for d in ("30", "60", "90")],
    }


async def noncompliant_placements(t: TileContext) -> TileData:
    need(t, "placements")
    n = (
        await t.db.execute(
            select(func.count()).where(
                P.id.in_(
                    scoped_placements(
                        t, P.compliance_blocked.is_(True), P.status.not_in(("ended", "dropped"))
                    )
                )
            )
        )
    ).scalar_one()
    return {"value": int(n), "link": le_link(t, "/placements", compliance_blocked="true")}


async def sla_bgv_turnaround(t: TileContext) -> TileData:
    need(t, "sla_clocks")
    s = T.sla_clocks.c
    f = t.filters
    rows = dict(
        (
            await t.db.execute(
                select(s.status, func.count())
                .where(s.subject_type == "bgv", s.started_at >= f.start, s.started_at < f.end)
                .group_by(s.status)
            )
        )
        .tuples()
        .all()
    )
    keys = ("healthy", "at_risk", "breached", "met")
    return {
        "rows": [
            {"key": k, "label": k.replace("_", " ").capitalize(), "value": int(rows.get(k, 0))} for k in keys
        ]
    }
