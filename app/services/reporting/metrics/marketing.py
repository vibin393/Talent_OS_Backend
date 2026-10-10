# ruff: noqa: E501
"""Marketing dashboard tiles (Epic 52 A4; Epic 39 data). Campaign data is tenant-wide within ``campaign:read`` (the
owner's list rule). The funnel comes from ``campaigns_service.funnel`` (the owner's aggregate SQL); opens are counted
only where the ``tracking`` consent allowed a pixel, so they are shown apart from the funnel as approximate."""

from datetime import timedelta

from sqlalchemy import func, select

from app.models.campaigns import Campaign, CampaignAttribution, CampaignMessage, EngagementEvent, Enrollment
from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.p3common import label, need, range_link

# NFR-34 thresholds (percent of delivered messages).
COMPLAINT_RED_PCT = 0.1
BOUNCE_RED_PCT = 2.0
FUNNEL_STEPS = ("audience", "sent", "delivered", "clicked", "applied", "hired")
STEP_LABELS = {
    "audience": "Recipients",
    "sent": "Sent",
    "delivered": "Delivered",
    "clicked": "Clicked",
    "applied": "Applied",
    "hired": "Hired",
}


async def _funnel(t: TileContext) -> object:
    from app.services import campaigns_service

    return await campaigns_service.funnel(t.ctx, t.db, t.filters.start, t.filters.end)


async def campaign_funnel(t: TileContext) -> TileData:
    need(t, "campaign_messages", "engagement_events")
    out = await _funnel(t)
    counts = out.counts.model_dump()  # type: ignore[attr-defined]
    rows = []
    first = previous = None
    for key in FUNNEL_STEPS:
        n = int(counts.get(key, 0))
        rows.append({
            "key": key, "label": STEP_LABELS[key], "value": n,
            "pct_of_previous": None if previous is None else pct(n, previous),
            "pct_of_first": None if first is None else pct(n, first),
        })  # fmt: skip
        first = n if first is None else first
        previous = n
    return {
        "value": rows[0]["value"],
        "rows": rows,
        "series_keys": [
            {
                "key": "opened",
                "label": "Opened (approximate, tracking consent only)",
                "value": int(counts.get("opened", 0)),
            }
        ],
        "link": range_link(t, "/marketing/campaigns"),
    }


async def suppressions_by_reason(t: TileContext) -> TileData:
    need(t, "campaign_messages")
    out = await _funnel(t)
    reasons = dict(out.suppressed_by_reason)  # type: ignore[attr-defined]
    rows = [
        {"key": k, "label": label(k), "value": int(v)}
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])
    ]
    return {"value": sum(r["value"] for r in rows), "rows": rows}


async def complaint_bounce_rate(t: TileContext) -> TileData:
    need(t, "campaign_messages", "engagement_events")
    f = t.filters
    m, e = CampaignMessage, EngagementEvent
    delivered = dict(
        (await t.db.execute(
            select(m.channel, func.count()).where(m.status.in_(("sent", "delivered")), m.created_at >= f.start, m.created_at < f.end).group_by(m.channel)
        )).tuples().all()
    )  # fmt: skip
    events = (
        await t.db.execute(
            select(m.channel, e.kind, func.count(func.distinct(e.campaign_message_id)))
            .join(m, m.id == e.campaign_message_id)
            .where(e.kind.in_(("bounced", "complained")), e.occurred_at >= f.start, e.occurred_at < f.end)
            .group_by(m.channel, e.kind)
        )
    ).all()
    by: dict[str, dict[str, int]] = {}
    for channel, kind, n in events:
        by.setdefault(channel, {})[kind] = int(n)
    rows = []
    for channel in sorted(set(delivered) | set(by)):
        base = int(delivered.get(channel, 0)) + by.get(channel, {}).get("bounced", 0)
        complaint = pct(by.get(channel, {}).get("complained", 0), base)
        bounce = pct(by.get(channel, {}).get("bounced", 0), base)
        rows.append({
            "key": channel, "label": label(channel), "complaint_rate": complaint, "bounce_rate": bounce,
            "red": bool((complaint or 0) > COMPLAINT_RED_PCT or (bounce or 0) > BOUNCE_RED_PCT),
        })  # fmt: skip
    worst = max((r["complaint_rate"] or 0.0 for r in rows), default=None)
    return {"value": worst, "unit": "%", "rows": rows}


async def sequence_enrolments(t: TileContext) -> TileData:
    need(t, "enrollments")
    counts = dict(
        (await t.db.execute(select(Enrollment.status, func.count()).group_by(Enrollment.status)))
        .tuples()
        .all()
    )
    keys = ("active", "completed", "exited", "suppressed")
    rows = [{"key": k, "label": label(k), "value": int(counts.get(k, 0))} for k in keys]
    return {"value": rows[0]["value"], "rows": rows, "link": {"path": "/marketing/sequences", "query": {}}}


async def campaign_attributed_applications(t: TileContext) -> TileData:
    """Last-touch attribution within 30 days (owner rule); SM-22 = applications per 1,000 delivered."""
    need(t, "campaign_attributions", "campaign_messages")
    f = t.filters
    a = CampaignAttribution
    n = int(
        (
            await t.db.execute(
                select(func.count()).where(
                    a.attributed_at >= f.start,
                    a.attributed_at < f.end,
                    a.touched_at >= a.attributed_at - timedelta(days=30),
                )
            )
        ).scalar_one()
    )
    out = await _funnel(t)
    delivered = int(out.counts.delivered)  # type: ignore[attr-defined]
    per_k = round(1000.0 * n / delivered, 2) if delivered else None
    campaigns = int(
        (
            await t.db.execute(
                select(func.count())
                .select_from(Campaign)
                .where(Campaign.started_at >= f.start, Campaign.started_at < f.end)
            )
        ).scalar_one()
    )
    return {
        "value": n,
        "unit": "applications",
        "rows": [
            {"key": "per_1000_delivered", "label": "Per 1,000 delivered (SM-22)", "value": per_k},
            {"key": "campaigns_started", "label": "Campaigns started", "value": campaigns},
        ],
    }
