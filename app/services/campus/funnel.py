# ruff: file-ignore[line-too-long]
"""Campus funnel by Institution (Epic 43 story 43.6, FR-221; consumed by Epic 52's campus tile).

Per Institution, the number of registrations whose milestone timestamp is set: registered, eligible, assessed,
shortlisted, interviewed, offered, accepted, joined, plus reneged. Respects the caller's ``campus:read`` scope (drive
owner / team / BU); erased subjects' rows are gone, so the counts are recomputed from what remains (no snapshot).
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.campus import CampusDrive, Institution, Registration
from app.schemas.campus import FunnelOut, FunnelRow
from app.services.campus.common import drive_for, drive_scope

STAGES = ("registered", "eligible", "assessed", "shortlisted", "interviewed", "offered", "accepted", "joined")
COLUMNS = {
    "registered": Registration.registered_at,
    "eligible": Registration.eligible_at,
    "assessed": Registration.assessed_at,
    "shortlisted": Registration.shortlisted_at,
    "interviewed": Registration.interviewed_at,
    "offered": Registration.offered_at,
    "accepted": Registration.accepted_at,
    "joined": Registration.joined_at,
    "reneged": Registration.reneged_at,
}


async def funnel(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID | None, start: date | None, end: date | None) -> FunnelOut:
    stmt = select(
        Registration.institution_id,
        Institution.name,
        *[func.count(col).label(key) for key, col in COLUMNS.items()],
    ).join(Institution, Institution.id == Registration.institution_id)
    if drive_id is not None:
        drive = await drive_for(c, db, drive_id)
        stmt = stmt.where(Registration.drive_id == drive.id)
    else:
        cond = drive_scope(c)
        if cond is not None:
            stmt = stmt.where(Registration.drive_id.in_(select(CampusDrive.id).where(cond)))
    if start is not None:
        stmt = stmt.where(Registration.registered_at >= datetime.combine(start, time.min, tzinfo=UTC))
    if end is not None:
        stmt = stmt.where(Registration.registered_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC))
    stmt = stmt.group_by(Registration.institution_id, Institution.name).order_by(Institution.name)
    rows: list[FunnelRow] = []
    totals = dict.fromkeys(COLUMNS, 0)
    for r in (await db.execute(stmt)).mappings():
        counts = {key: int(r[key] or 0) for key in COLUMNS}
        for k, v in counts.items():
            totals[k] += v
        rows.append(FunnelRow(institution_id=r["institution_id"], institution_name=str(r["name"]), **counts))
    return FunnelOut(
        drive_id=drive_id,
        stages=list(STAGES),
        rows=rows,
        totals=FunnelRow(institution_id=uuid.UUID(int=0), institution_name="All institutions", **totals),
    )
