# ruff: file-ignore[line-too-long]
"""Campus dashboard tiles (Epic 52 A4; Epic 43 data). Drives are scoped like ``GET /campus/drives``
(``campus:read`` on the drive owner); the institution funnel is the owner's ``campus_service.funnel``."""

from typing import Any

from sqlalchemy import func, select

from app.models.campus import CampusDrive, EventCheckin, QueueEntry, RecruitmentEvent, Registration
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p3common import days_between, median, need, range_link

STAGES = (
    "registered",
    "eligible",
    "assessed",
    "shortlisted",
    "interviewed",
    "offered",
    "accepted",
    "joined",
    "reneged",
)


def _drive_ids(t: TileContext) -> Any:
    from app.services.campus.common import drive_scope

    stmt = select(CampusDrive.id)
    cond = drive_scope(t.ctx)
    return stmt if cond is None else stmt.where(cond)


async def campus_funnel_by_institution(t: TileContext) -> TileData:
    need(t, "registrations", "campus_drives")
    from app.services import campus_service

    out = await campus_service.funnel(t.ctx, t.db, None, t.filters.from_date, t.filters.to_date)
    rows = [{"institution": r.institution_name, **{k: getattr(r, k) for k in STAGES}} for r in out.rows]
    return {
        "value": out.totals.registered,
        "rows": rows,
        "series_keys": [{"key": k, "label": k.replace("_", " ").capitalize()} for k in STAGES],
        "link": range_link(t, "/campus/drives"),
    }


async def drive_cycle_time(t: TileContext) -> TileData:
    """SM-25: median days from registration close to the first offer released, over drives whose first offer falls
    in the range."""
    need(t, "registrations", "campus_drives")
    f = t.filters
    first_offer = func.min(Registration.offered_at)
    rows = (
        await t.db.execute(
            select(CampusDrive.registration_closes_at, first_offer)
            .join(Registration, Registration.drive_id == CampusDrive.id)
            .where(
                CampusDrive.id.in_(_drive_ids(t)),
                Registration.offered_at.is_not(None),
                CampusDrive.registration_closes_at.is_not(None),
            )
            .group_by(CampusDrive.id, CampusDrive.registration_closes_at)
            .having(first_offer >= f.start, first_offer < f.end)
        )
    ).all()
    values = [max(0.0, days_between(closes, offered)) for closes, offered in rows]
    return {"value": median(values), "unit": "days", "n": len(values)}


async def event_checkins_queue(t: TileContext) -> TileData:
    need(t, "event_checkins", "queue_entries")
    from app.core.permissions import scope_condition

    p = t.principal
    events = select(RecruitmentEvent.id)
    cond = scope_condition(
        p, "event:manage" if p.has("event:manage") else "campus:read", RecruitmentEvent.owner_id
    )
    if cond is not None:
        events = events.where(cond)
    f = t.filters
    checked = int(
        (
            await t.db.execute(
                select(func.count()).where(
                    EventCheckin.event_id.in_(events),
                    EventCheckin.checked_in_at >= f.start,
                    EventCheckin.checked_in_at < f.end,
                )
            )
        ).scalar_one()
    )
    q = QueueEntry
    interviewed, avg_wait = (
        await t.db.execute(
            select(
                func.count().filter(q.status == "done"),
                func.avg(func.extract("epoch", q.started_at - q.created_at)).filter(
                    q.started_at.is_not(None)
                ),
            ).where(q.event_id.in_(events), q.created_at >= f.start, q.created_at < f.end)
        )
    ).one()
    wait_min = None if avg_wait is None else round(float(avg_wait) / 60.0, 1)
    return {
        "value": checked,
        "unit": "checked in",
        "rows": [
            {"key": "checked_in", "label": "Checked in", "value": checked},
            {"key": "interviewed", "label": "Interviewed", "value": int(interviewed)},
            {"key": "avg_wait_minutes", "label": "Average wait (minutes)", "value": wait_min},
        ],
        "link": {"path": "/campus/events", "query": {}},
    }
