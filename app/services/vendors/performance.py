"""Vendor performance (Epic 17 A9, FR-99, AD-33): computed only from records, never stored.

Releases received, submissions, acceptance / duplicate %, submission -> interview and interview -> offer
ratios
(through the submissions' Applications), placements, median hours from release to first submission, SLA
breaches of the ``vendor_submission`` review clock and placements ended within 30 days of start.
"""

import statistics
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.interviews import Interview
from app.models.offers import Offer
from app.models.placements import Placement
from app.models.sla import SlaClock
from app.models.vendors import Vendor, VendorRelease, VendorSubmission
from app.schemas.vendors import VendorScorecard
from app.services.vendors.common import get_vendor, now


def _pct(num: int, den: int) -> Decimal | None:
    if den == 0:
        return None
    return (Decimal(num) * 100 / Decimal(den)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _window(period_from: date | None, period_to: date | None) -> tuple[date, date, datetime, datetime]:
    to = period_to or now().date()
    frm = period_from or (to - timedelta(days=90))
    return (
        frm,
        to,
        datetime.combine(frm, time.min, UTC),
        datetime.combine(to + timedelta(days=1), time.min, UTC),
    )


async def vendor_scorecard(
    db: AsyncSession, vendor_id: uuid.UUID, period_from: date | None = None, period_to: date | None = None
) -> VendorScorecard:
    frm, to, start, end = _window(period_from, period_to)
    releases = list(
        (
            await db.execute(
                select(VendorRelease).where(
                    VendorRelease.vendor_id == vendor_id,
                    VendorRelease.released_at >= start,
                    VendorRelease.released_at < end,
                )
            )
        ).scalars()
    )
    subs = list(
        (
            await db.execute(
                select(VendorSubmission).where(
                    VendorSubmission.vendor_id == vendor_id,
                    VendorSubmission.created_at >= start,
                    VendorSubmission.created_at < end,
                    VendorSubmission.status != "withdrawn",
                )
            )
        ).scalars()
    )
    accepted = [s for s in subs if s.status == "accepted"]
    duplicates = [s for s in subs if s.status == "duplicate"]
    app_ids = [s.application_id for s in accepted if s.application_id is not None]
    interviewed: set[uuid.UUID] = set()
    offered: set[uuid.UUID] = set()
    if app_ids:
        interviewed = set(
            (
                await db.execute(
                    select(Interview.application_id).where(
                        Interview.application_id.in_(app_ids), Interview.status != "cancelled"
                    )
                )
            ).scalars()
        )
        offered = set(
            (
                await db.execute(select(Offer.application_id).where(Offer.application_id.in_(app_ids)))
            ).scalars()
        )
    placements = list(
        (
            await db.execute(
                select(Placement).where(
                    Placement.vendor_id == vendor_id,
                    Placement.created_at >= start,
                    Placement.created_at < end,
                )
            )
        ).scalars()
    )
    early = sum(
        1
        for p in placements
        if p.end_date is not None
        and p.actual_start_date is not None
        and (p.end_date - p.actual_start_date).days <= 30
    )
    first_by_release: dict[uuid.UUID, datetime] = {}
    if releases:
        rows = await db.execute(
            select(VendorSubmission.vendor_release_id, func.min(VendorSubmission.created_at))
            .where(VendorSubmission.vendor_release_id.in_([r.id for r in releases]))
            .group_by(VendorSubmission.vendor_release_id)
        )
        first_by_release = dict(rows.tuples().all())
    hours = [
        (first_by_release[r.id] - r.released_at).total_seconds() / 3600
        for r in releases
        if r.id in first_by_release
    ]
    median = Decimal(str(statistics.median(hours))).quantize(Decimal("0.1"), ROUND_HALF_UP) if hours else None
    breaches = 0
    if subs:
        breaches = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(SlaClock)
                    .where(
                        SlaClock.subject_type == "vendor_submission",
                        SlaClock.subject_id.in_([s.id for s in subs]),
                        SlaClock.status == "breached",
                    )
                )
            ).scalar_one()
        )
    return VendorScorecard(
        vendor_id=vendor_id,
        period_from=frm,
        period_to=to,
        releases_received=len(releases),
        submissions=len(subs),
        accepted=len(accepted),
        acceptance_pct=_pct(len(accepted), len(subs)),
        duplicates=len(duplicates),
        duplicate_pct=_pct(len(duplicates), len(subs)),
        interviews=len(interviewed),
        submission_to_interview_pct=_pct(len(interviewed), len(accepted)),
        offers=len(offered),
        interview_to_offer_pct=_pct(
            len(offered & interviewed) if interviewed else len(offered), len(interviewed)
        ),
        placements=len(placements),
        median_hours_to_first_submission=median,
        sla_breaches=breaches,
        early_exits=early,
    )


async def scorecard(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    vendor_id: uuid.UUID,
    period_from: date | None = None,
    period_to: date | None = None,
) -> VendorScorecard:
    v = await get_vendor(db, vendor_id)
    out = await vendor_scorecard(db, v.id, period_from, period_to)
    return out.model_copy(update={"vendor_name": v.legal_name})


async def league(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    period_from: date | None = None,
    period_to: date | None = None,
) -> list[VendorScorecard]:
    vendors = (
        (await db.execute(select(Vendor).where(Vendor.archived_at.is_(None)).order_by(Vendor.legal_name)))
        .scalars()
        .all()
    )
    out = []
    for v in vendors:
        card = await vendor_scorecard(db, v.id, period_from, period_to)
        out.append(card.model_copy(update={"vendor_name": v.legal_name}))
    out.sort(key=lambda c: (-(c.acceptance_pct or Decimal(0)), -c.placements, c.vendor_name or ""))
    return out
