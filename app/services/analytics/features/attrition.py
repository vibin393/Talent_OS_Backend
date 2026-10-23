# ruff: file-ignore[line-too-long]
"""Attrition extractor (Epic 48 A3, FR-243). Sensitive: reads are gated by ``prediction:read_attrition``.

* Subjects scored: Placements ``active``.
* Label: 1 = ended >= 7 days before ``planned_end_date`` with offboarding reason in {client_termination,
  worker_resignation, misconduct}, or the latest extension ``declined``; 0 = ``end_of_term`` completed, extended
  or converted.
* Training ``as_of`` = ``outcome_at - 60 days``, bounded below by ``start_date + 30 days``.

``extract`` is pure: every count (timesheet rejections, leave, bench periods, extensions, client early ends)
only uses facts dated <= ``as_of``.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.analytics.features import common
from app.services.analytics.features.common import (
    FeatureRow,
    PlacementFact,
    ScoringRow,
    TrainingRow,
    days_between,
    to_dt,
)

EARLY_REASONS = ("client_termination", "worker_resignation", "misconduct")
STAY_REASONS = ("end_of_term", "converted")


@dataclasses.dataclass(frozen=True, slots=True)
class Context:
    rejections: Mapping[uuid.UUID, Sequence[date]]
    bench: Mapping[uuid.UUID, Sequence[datetime]]
    pay: Mapping[uuid.UUID, tuple[float, tuple[str, str]]]
    pay_medians: Mapping[tuple[str, str], float]
    leave: Mapping[uuid.UUID, Sequence[tuple[date, float, str, datetime, datetime | None]]]
    extensions: Mapping[uuid.UUID, Sequence[tuple[str, datetime]]]
    client_ends: Mapping[uuid.UUID, Sequence[tuple[date, bool]]]


def extract(p: PlacementFact, as_of: datetime, ctx: Context) -> FeatureRow:
    day = as_of.date()
    window = day - timedelta(days=90)
    start = p.actual_start_date or p.planned_start_date
    rejections = [d for d in ctx.rejections.get(p.id, ()) if window < d <= day]
    leave = ctx.leave.get(p.id, ())
    taken = sum(
        days
        for start_d, days, status, _c, decided in leave
        if status == "approved" and window < start_d <= day and (decided is None or decided <= as_of)
    )
    pending = sum(
        1
        for _s, _d, status, created, decided in leave
        if created <= as_of
        and (decided is None or decided > as_of)
        and status
        in ("requested", "approved", "rejected", "cancelled", "withdrawn", "cancellation_requested")
    )
    exts = [
        s for s, at in ctx.extensions.get(p.id, ()) if s == "confirmed" and at is not None and at <= as_of
    ]
    ends = [early for end, early in ctx.client_ends.get(p.client_id, ()) if end <= day]
    pay = ctx.pay.get(p.id)
    med = ctx.pay_medians.get(pay[1]) if pay is not None else None
    return {
        "tenure": days_between(start, as_of),
        "timesheet_rejections": float(len(rejections)),
        "bench_entries": float(sum(1 for at in ctx.bench.get(p.candidate_id, ()) if at <= as_of)),
        "pay_rate_vs_median": (pay[0] / med) if pay is not None and med else None,
        "leave_days": float(taken),
        "pending_leave": float(pending),
        "prior_extensions": float(len(exts)),
        "client_early_end_rate": (sum(1 for e in ends if e) / len(ends)) if ends else None,
        "days_to_planned_end": days_between(as_of, p.planned_end_date)
        if p.planned_end_date is not None
        else None,
    }


def outcome(
    p: PlacementFact, reason: tuple[str, datetime | None] | None, exts: Sequence[tuple[str, datetime]]
) -> tuple[float, datetime] | None:
    latest_ext = max(exts, key=lambda e: e[1]) if exts else None
    if latest_ext is not None and latest_ext[0] == "declined":
        return 1.0, latest_ext[1]
    end = to_dt(p.end_date)
    if reason is not None and end is not None:
        why = reason[0]
        early = (
            p.planned_end_date is not None
            and p.end_date is not None
            and (p.planned_end_date - p.end_date).days >= 7
        )
        if why in EARLY_REASONS and early:
            return 1.0, end
        if why in STAY_REASONS:
            return 0.0, end
    if latest_ext is not None and latest_ext[0] == "confirmed":
        return 0.0, latest_ext[1]
    return None


def training_as_of(p: PlacementFact, outcome_at: datetime) -> datetime | None:
    start = to_dt(p.actual_start_date or p.planned_start_date)
    as_of = outcome_at - timedelta(days=60)
    if start is not None:
        as_of = max(as_of, start + timedelta(days=30))
    return as_of if as_of < outcome_at else None


async def _context(db: AsyncSession, placements: Sequence[PlacementFact]) -> Context:
    pids = [p.id for p in placements]
    pay, medians = await common.pay_rates(db)
    return Context(
        rejections=await common.timesheet_rejections(db, pids),
        bench=await common.bench_entries(db, {p.candidate_id for p in placements}),
        pay=pay,
        pay_medians=medians,
        leave=await common.leave_requests(db, pids),
        extensions=await common.extensions(db, pids),
        client_ends=await common.client_placement_ends(db),
    )


async def training_rows(db: AsyncSession, now: datetime, history_months: int) -> list[TrainingRow]:
    since = now - timedelta(days=30 * history_months)
    placements = [
        p
        for p in await common.load_placements(db)
        if p.actual_start_date is not None or p.status in ("active", "offboarding", "ended")
    ]
    reasons = await common.offboarding_reasons(db)
    ctx = await _context(db, placements)
    rows: list[TrainingRow] = []
    for p in placements:
        res = outcome(p, reasons.get(p.id), ctx.extensions.get(p.id, ()))
        if res is None:
            continue
        lab, outcome_at = res
        if outcome_at < since:
            continue
        as_of = training_as_of(p, outcome_at)
        if as_of is None:
            continue
        rows.append(TrainingRow(p.id, p.candidate_id, as_of, outcome_at, extract(p, as_of, ctx), lab))
    return rows


async def scoring_rows(
    db: AsyncSession, now: datetime, subject_ids: Sequence[uuid.UUID] | None
) -> list[ScoringRow]:
    placements = await common.load_placements(db, subject_ids, statuses=("active",))
    if not placements:
        return []
    ctx = await _context(db, placements)
    return [ScoringRow("placement", p.id, p.candidate_id, extract(p, now, ctx)) for p in placements]
