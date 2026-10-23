# ruff: file-ignore[line-too-long]
"""Offer-drop extractor (Epic 48 A3, FR-242).

* Subjects scored: Offers ``accepted`` without a Placement yet (subject ``offer``), and Placements ``pre_joining``
  / ``joining_confirmed`` (subject ``placement``).
* Label: 1 = the Placement from the accepted Offer was ``dropped`` with ``drop_reason in {declined, no_show}``;
  0 = it reached ``active``.
* Training ``as_of`` = ``min(accepted_at + 7 days, outcome_at - 1 day)``.

``extract`` is pure and ignores every fact timestamped after ``as_of`` (offer revisions, onboarding task
completions, BGV resolution, counter-offer flag). The compensation change is a % of the current CTC (same
currency only) and is only ever displayed as a band.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.analytics.features import common
from app.services.analytics.features.common import (
    CandidateFact,
    FeatureRow,
    OfferFact,
    PlacementFact,
    ScoringRow,
    TrainingRow,
    days_between,
)

DROP_REASONS = ("declined", "no_show")
PRE_JOINING = ("pre_joining", "joining_confirmed")


@dataclasses.dataclass(frozen=True, slots=True)
class Subject:
    offer: OfferFact | None
    placement: PlacementFact | None
    candidate: CandidateFact | None
    tasks: Sequence[tuple[datetime, datetime | None]] = ()
    bgv: Sequence[tuple[datetime, datetime | None]] = ()


def extract(s: Subject, as_of: datetime) -> FeatureRow:
    o, p, c = s.offer, s.placement, s.candidate
    accepted_at = o.accepted_at if o is not None else None
    start = (o.start_date if o is not None else None) or (p.planned_start_date if p is not None else None)
    revisions = None
    if o is not None:
        revisions = float(max(sum(1 for t in o.version_times if t <= as_of) - 1, 0))
    comp = None
    if o is not None and c is not None and o.annual_pay and c.annual_ctc and o.pay_currency == c.ctc_currency:
        comp = (o.annual_pay - c.annual_ctc) / c.annual_ctc * 100.0
    tasks = [(created, done) for created, done in s.tasks if created <= as_of]
    completion = (
        (sum(1 for _, done in tasks if done is not None and done <= as_of) / len(tasks)) if tasks else None
    )
    bgv_pending = None
    if p is not None:
        live = [(created, resolved) for created, resolved in s.bgv if created <= as_of]
        bgv_pending = 1.0 if any(resolved is None or resolved > as_of for _, resolved in live) else 0.0
    counter = None
    if p is not None:
        counter = 1.0 if p.counter_offer_at is not None and p.counter_offer_at <= as_of else 0.0
    work_city = (p.work_city if p is not None else None) or (o.location_city if o is not None else None)
    different = None
    if c is not None and c.city and work_city:
        different = 0.0 if c.city == work_city else 1.0
    return {
        "notice_period": float(c.notice_period_days)
        if c is not None and c.notice_period_days is not None
        else None,
        "days_since_acceptance": days_between(accepted_at, as_of),
        "joining_lead_time": days_between(accepted_at, start)
        if accepted_at is not None and start is not None
        else None,
        "offer_revisions": revisions,
        "compensation_change": comp,
        "prejoining_completion": completion,
        "bgv_pending": bgv_pending,
        "counter_offer": counter,
        "different_city": different,
    }


def outcome(p: PlacementFact) -> tuple[float, datetime] | None:
    if p.status == "dropped" and p.drop_reason in DROP_REASONS:
        at = p.status_times.get("dropped")
        return (1.0, at) if at is not None else None
    at = p.status_times.get("active") or common.to_dt(p.actual_start_date)
    if at is not None and p.status in ("active", "offboarding", "ended"):
        return 0.0, at
    return None


def training_as_of(accepted_at: datetime, outcome_at: datetime) -> datetime:
    return min(accepted_at + timedelta(days=7), outcome_at - timedelta(days=1))


async def _subjects(
    db: AsyncSession, placements: Sequence[PlacementFact], offers: Mapping[uuid.UUID, OfferFact]
) -> tuple[dict[uuid.UUID, CandidateFact], dict, dict]:  # type: ignore[type-arg]
    cids = {p.candidate_id for p in placements} | {o.candidate_id for o in offers.values()}
    pids = [p.id for p in placements]
    return (
        await common.load_candidates(db, cids),
        await common.onboarding_tasks(db, pids),
        await common.bgv_cases(db, pids),
    )


async def training_rows(db: AsyncSession, now: datetime, history_months: int) -> list[TrainingRow]:
    since = now - timedelta(days=30 * history_months)
    placements = [p for p in await common.load_placements(db) if p.offer_id is not None]
    offers = await common.load_offers(db, ids=[p.offer_id for p in placements if p.offer_id is not None])
    cands, tasks, bgv = await _subjects(db, placements, offers)
    rows: list[TrainingRow] = []
    for p in placements:
        o = offers.get(p.offer_id) if p.offer_id is not None else None
        res = outcome(p)
        if o is None or o.accepted_at is None or res is None:
            continue
        lab, outcome_at = res
        if outcome_at < since or outcome_at <= o.accepted_at:
            continue
        as_of = training_as_of(o.accepted_at, outcome_at)
        subject = Subject(o, p, cands.get(p.candidate_id), tasks.get(p.id, ()), bgv.get(p.id, ()))
        rows.append(TrainingRow(p.id, p.candidate_id, as_of, outcome_at, extract(subject, as_of), lab))
    return rows


async def scoring_rows(
    db: AsyncSession, now: datetime, subject_ids: Sequence[uuid.UUID] | None, subject_kind: str | None = None
) -> list[ScoringRow]:
    rows: list[ScoringRow] = []
    if subject_kind in (None, "placement"):
        placements = await common.load_placements(db, subject_ids, statuses=PRE_JOINING)
        offers = await common.load_offers(db, ids=[p.offer_id for p in placements if p.offer_id is not None])
        cands, tasks, bgv = await _subjects(db, placements, offers)
        for p in placements:
            o = offers.get(p.offer_id) if p.offer_id is not None else None
            s = Subject(o, p, cands.get(p.candidate_id), tasks.get(p.id, ()), bgv.get(p.id, ()))
            rows.append(ScoringRow("placement", p.id, p.candidate_id, extract(s, now)))
    if subject_kind in (None, "offer"):
        offers = await common.load_offers(db, ids=subject_ids)
        offers = {k: o for k, o in offers.items() if o.status == "accepted"}
        with_placement = await common.offers_with_placement(db, list(offers))
        pending = {k: o for k, o in offers.items() if k not in with_placement}
        cands = await common.load_candidates(db, {o.candidate_id for o in pending.values()})
        for o in pending.values():
            rows.append(
                ScoringRow(
                    "offer",
                    o.offer_id,
                    o.candidate_id,
                    extract(Subject(o, None, cands.get(o.candidate_id)), now),
                )
            )
    return rows
