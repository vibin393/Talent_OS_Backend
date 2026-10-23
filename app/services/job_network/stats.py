# ruff: file-ignore[line-too-long]
"""Per-listing daily aggregate counters (Epic 75, spec A8; FR-390). Counts only -- **no IP, user agent, cookie,
referrer or session id is stored anywhere**. Impressions / views / apply clicks are accumulated into
``network_listing_stats`` (one row per listing per day). Applications are never stored here: each Tenant counts its
own ``applications`` with ``source='job_network'``."""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.models.job_network import NetworkListing, NetworkListingStat
from app.schemas.job_network import ListingAnalyticsRow, NetworkAnalyticsOut

_FIELDS = ("impressions", "views", "apply_clicks")


async def _bump(pdb: AsyncSession, listing_id: uuid.UUID, field: str, n: int = 1) -> None:
    assert field in _FIELDS
    today = datetime.now(UTC).date()
    await pdb.execute(
        text(
            f"INSERT INTO network_listing_stats (listing_id, day, {field}) "
            "VALUES (:lid, :day, :n) "
            f"ON CONFLICT (listing_id, day) DO UPDATE SET {field} = network_listing_stats.{field} + :n"
        ),
        {"lid": str(listing_id), "day": today, "n": n},
    )


async def bump_view(pdb: AsyncSession, listing_id: uuid.UUID) -> None:
    await _bump(pdb, listing_id, "views")


async def bump_apply_click(pdb: AsyncSession, listing_id: uuid.UUID) -> None:
    await _bump(pdb, listing_id, "apply_clicks")


async def bump_impressions(pdb: AsyncSession, listing_ids: list[uuid.UUID]) -> None:
    for lid in listing_ids[:50]:
        await _bump(pdb, lid, "impressions")


async def listing_totals(pdb: AsyncSession, listing_id: uuid.UUID) -> dict[str, int]:
    row = (
        await pdb.execute(
            select(
                func.coalesce(func.sum(NetworkListingStat.impressions), 0),
                func.coalesce(func.sum(NetworkListingStat.views), 0),
                func.coalesce(func.sum(NetworkListingStat.apply_clicks), 0),
            ).where(NetworkListingStat.listing_id == listing_id)
        )
    ).first()
    return {"impressions": int(row[0]), "views": int(row[1]), "apply_clicks": int(row[2])} if row else {
        "impressions": 0, "views": 0, "apply_clicks": 0,
    }


async def flush_stats() -> int:
    """Beat safety-net (counters are written straight to the DB; nothing to flush when no Redis buffer is used)."""
    return 0


async def analytics(
    ctx: RequestContext, db: AsyncSession, *, date_from: date | None, date_to: date | None,
    demand_id: uuid.UUID | None = None,  # ruff: ignore[unused-function-argument] - accepted for API symmetry; network_listings has no demand_id to filter on
) -> NetworkAnalyticsOut:
    """Per-listing stats for **the caller's own** tenant only (``ctx.tenant_id``; never a request parameter). Platform
    session reads ``network_listing_stats`` filtered by the tenant's listings; ``applications`` come from the tenant's
    own ``applications`` rows (tenant session)."""
    assert ctx.tenant_id is not None
    tenant_id = ctx.tenant_id
    date_from = date_from or (datetime.now(UTC).date() - timedelta(days=30))
    date_to = date_to or datetime.now(UTC).date()

    async with platform_session() as pdb:
        stmt = (
            select(
                NetworkListing.id,
                NetworkListing.title,
                NetworkListing.posting_ref,
                func.coalesce(func.sum(NetworkListingStat.impressions), 0),
                func.coalesce(func.sum(NetworkListingStat.views), 0),
                func.coalesce(func.sum(NetworkListingStat.apply_clicks), 0),
            )
            .select_from(NetworkListing)
            .join(
                NetworkListingStat,
                (NetworkListingStat.listing_id == NetworkListing.id)
                & (NetworkListingStat.day >= date_from)
                & (NetworkListingStat.day <= date_to),
                isouter=True,
            )
            .where(NetworkListing.tenant_id == tenant_id)
            .group_by(NetworkListing.id, NetworkListing.title, NetworkListing.posting_ref)
        )
        raw = list((await pdb.execute(stmt)).all())

    # applications from the tenant's own records, keyed by source_ref = listing id.
    app_counts = await _application_counts(db, [str(r[0]) for r in raw])

    rows: list[ListingAnalyticsRow] = []
    tot_imp = tot_view = tot_click = tot_app = 0
    for lid, title, _posting_ref, imp, view, click in raw:
        apps = app_counts.get(str(lid), 0)
        rows.append(
            ListingAnalyticsRow(
                listing_id=lid, title=title, impressions=int(imp), views=int(view), apply_clicks=int(click),
                applications=apps, conversion=(round(apps / view, 4) if view else 0.0),
            )
        )
        tot_imp += int(imp)
        tot_view += int(view)
        tot_click += int(click)
        tot_app += apps
    totals = ListingAnalyticsRow(
        listing_id=uuid.UUID(int=0), title="Total", impressions=tot_imp, views=tot_view, apply_clicks=tot_click,
        applications=tot_app, conversion=(round(tot_app / tot_view, 4) if tot_view else 0.0),
    )
    return NetworkAnalyticsOut(rows=rows, totals=totals)


async def _application_counts(db: AsyncSession, listing_ids: list[str]) -> dict[str, int]:
    """Count the tenant's own applications tagged ``source='job_network'`` grouped by ``source_ref`` (= listing id).
    Reads only the caller tenant's rows (tenant session, RLS)."""
    if not listing_ids:
        return {}
    rows = (
        await db.execute(
            text(
                "SELECT source_ref, COUNT(*) FROM applications "
                "WHERE source = 'job_network' AND source_ref = ANY(:ids) GROUP BY source_ref"
            ),
            {"ids": listing_ids},
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


# convenience wrappers opening their own platform session (used by the public router / tasks)
async def record_view(listing_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await bump_view(pdb, listing_id)


async def record_apply_click(listing_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await bump_apply_click(pdb, listing_id)


async def record_impressions(listing_ids: list[uuid.UUID]) -> None:
    async with platform_session() as pdb:
        await bump_impressions(pdb, listing_ids)


__all__ = [
    "analytics",
    "bump_apply_click",
    "bump_impressions",
    "bump_view",
    "flush_stats",
    "listing_totals",
    "record_apply_click",
    "record_impressions",
    "record_view",
]
