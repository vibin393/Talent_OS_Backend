# ruff: file-ignore[line-too-long, unnecessary-comprehension]
"""Shared row types and read-only, set-based loaders (Epic 48 A3).

The loaders are this epic's own read-only query functions over the P1 / P2 models (spec contract: "where a
needed aggregate has no public function, this epic adds a read-only query function in its own
``features/common.py``"). Each runs a handful of set-based SELECTs per tenant -- never a per-row ORM loop -- and
returns plain facts with timestamps; the per-kind ``extract`` functions are pure and apply the ``as_of`` cut-off.
No loader reads a protected attribute, a sub-city location, a self-ID field or free text.
"""

import dataclasses
import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

FeatureRow = dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class TrainingRow:
    subject_id: uuid.UUID
    candidate_id: uuid.UUID | None
    as_of: datetime
    outcome_at: datetime
    features: FeatureRow
    label: float


@dataclasses.dataclass(frozen=True, slots=True)
class ScoringRow:
    subject_kind: str
    subject_id: uuid.UUID
    candidate_id: uuid.UUID | None
    features: FeatureRow


# ---------------------------------------------------------------- small helpers (pure)
def to_dt(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(value, time(0, 0), tzinfo=UTC)


def days_between(a: date | datetime | None, b: date | datetime | None) -> float | None:
    da, db_ = to_dt(a), to_dt(b)
    if da is None or db_ is None:
        return None
    return (db_ - da).total_seconds() / 86400.0


def num(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def median(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


def city_of(location: str | None) -> str | None:
    """City-level token of a free-form location ("Chennai, TN" -> "chennai"). Never below city."""
    if not location:
        return None
    head = location.split(",", 1)[0].strip().lower()
    return head[:60] or None


ANNUAL_FACTOR = {"hour": 2080.0, "day": 260.0, "month": 12.0, "year": 1.0}
CTC_FACTOR = {"hourly": 2080.0, "daily": 260.0, "monthly": 12.0, "annual": 1.0}


# ---------------------------------------------------------------- generic loaders
async def tenant_candidate_count(db: AsyncSession) -> int:
    from app.models.candidates import Candidate

    return int(
        (await db.execute(select(func.count(Candidate.id)).where(Candidate.erased_at.is_(None)))).scalar_one()
        or 0
    )


async def skill_holder_counts(db: AsyncSession, skill_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    from app.models.candidates import CandidateSkill

    ids = {s for s in skill_ids if s is not None}
    if not ids:
        return {}
    rows = await db.execute(
        select(CandidateSkill.skill_id, func.count(func.distinct(CandidateSkill.candidate_id)))
        .where(CandidateSkill.skill_id.in_(ids))
        .group_by(CandidateSkill.skill_id)
    )
    return {r[0]: int(r[1]) for r in rows}


async def client_feedback_delays(db: AsyncSession) -> dict[uuid.UUID, list[tuple[datetime, float]]]:
    """Per client: (feedback received_at, days from the submission being sent)."""
    from app.models.pipeline import ClientFeedback, Submission

    rows = await db.execute(
        select(Submission.client_id, Submission.sent_at, ClientFeedback.received_at)
        .join(ClientFeedback, ClientFeedback.submission_id == Submission.id)
        .where(Submission.sent_at.is_not(None))
    )
    out: dict[uuid.UUID, list[tuple[datetime, float]]] = defaultdict(list)
    for client_id, sent_at, received_at in rows:
        delay = days_between(sent_at, received_at)
        if delay is not None and delay >= 0:
            out[client_id].append((to_dt(received_at), delay))  # type: ignore[arg-type]
    return out


async def first_placement_at(db: AsyncSession) -> dict[uuid.UUID, datetime]:
    """Demand id -> created_at of its first Placement (the fill moment)."""
    from app.models.placements import Placement

    rows = await db.execute(
        select(Placement.demand_id, func.min(Placement.created_at))
        .where(Placement.demand_id.is_not(None))
        .group_by(Placement.demand_id)
    )
    return {r[0]: to_dt(r[1]) for r in rows if r[1] is not None}  # type: ignore[misc]


@dataclasses.dataclass(frozen=True, slots=True)
class DemandFact:
    id: uuid.UUID
    client_id: uuid.UUID
    status: str
    opened_at: datetime | None
    positions: int
    priority: str
    employment_type: str | None
    work_mode: str | None
    city: str | None
    min_experience: float | None
    bill_rate: float | None
    rate_key: tuple[str | None, str | None]
    skill_ids: tuple[uuid.UUID, ...]
    required_skills: int
    primary_user_id: uuid.UUID | None
    filled_at: datetime | None
    closed_at: datetime | None


async def load_demands(db: AsyncSession, ids: Sequence[uuid.UUID] | None = None) -> list[DemandFact]:
    from app.models.demands import Demand, DemandAssignment, DemandSkill

    stmt = select(Demand).where(Demand.opened_at.is_not(None))
    if ids is not None:
        stmt = stmt.where(Demand.id.in_(list(ids)))
    demands = list((await db.execute(stmt)).scalars())
    if not demands:
        return []
    dids = [d.id for d in demands]
    skills: dict[uuid.UUID, list[tuple[uuid.UUID | None, str]]] = defaultdict(list)
    for demand_id, skill_id, kind in await db.execute(
        select(DemandSkill.demand_id, DemandSkill.skill_id, DemandSkill.kind).where(
            DemandSkill.demand_id.in_(dids)
        )
    ):
        skills[demand_id].append((skill_id, kind))
    primary: dict[uuid.UUID, uuid.UUID] = {}
    for demand_id, user_id in await db.execute(
        select(DemandAssignment.demand_id, DemandAssignment.user_id)
        .where(
            DemandAssignment.demand_id.in_(dids),
            DemandAssignment.user_id.is_not(None),
            DemandAssignment.removed_at.is_(None),
        )
        .order_by(DemandAssignment.is_primary.desc(), DemandAssignment.assigned_at)
    ):
        primary.setdefault(demand_id, user_id)
    filled = await first_placement_at(db)
    out: list[DemandFact] = []
    for d in demands:
        sk = skills.get(d.id, [])
        closed = to_dt(d.updated_at) if d.status in ("filled", "closed", "cancelled") else None
        out.append(
            DemandFact(
                id=d.id,
                client_id=d.client_id,
                status=d.status,
                opened_at=to_dt(d.opened_at),
                positions=int(d.positions or 1),
                priority=d.priority,
                employment_type=d.employment_type,
                work_mode=d.work_mode,
                city=city_of(d.location),
                min_experience=num(d.min_experience_years),
                bill_rate=num(d.bill_rate),
                rate_key=(d.currency, d.rate_unit),
                skill_ids=tuple(s for s, _ in sk if s is not None),
                required_skills=sum(1 for _, k in sk if k == "mandatory"),
                primary_user_id=primary.get(d.id),
                filled_at=filled.get(d.id),
                closed_at=closed,
            )
        )
    return out


async def demand_rate_medians(db: AsyncSession) -> dict[tuple[str | None, str | None], float]:
    from app.models.demands import Demand

    by_key: dict[tuple[str | None, str | None], list[float]] = defaultdict(list)
    for currency, unit, rate in await db.execute(
        select(Demand.currency, Demand.rate_unit, Demand.bill_rate).where(Demand.bill_rate.is_not(None))
    ):
        by_key[currency, unit].append(float(rate))
    return {k: m for k, v in by_key.items() if (m := median(v))}


async def assignment_intervals(
    db: AsyncSession,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, datetime, datetime | None]]]:
    """User id -> [(demand id, assigned_at, removed_at)] (recruiter load at any as_of)."""
    from app.models.demands import DemandAssignment

    out: dict[uuid.UUID, list[tuple[uuid.UUID, datetime, datetime | None]]] = defaultdict(list)
    for demand_id, user_id, assigned_at, removed_at in await db.execute(
        select(
            DemandAssignment.demand_id,
            DemandAssignment.user_id,
            DemandAssignment.assigned_at,
            DemandAssignment.removed_at,
        ).where(DemandAssignment.user_id.is_not(None))
    ):
        out[user_id].append((demand_id, to_dt(assigned_at), to_dt(removed_at)))  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------- offers / placements
@dataclasses.dataclass(frozen=True, slots=True)
class OfferFact:
    offer_id: uuid.UUID
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    accepted_at: datetime | None
    version_times: tuple[datetime, ...]
    start_date: date | None
    annual_pay: float | None
    pay_currency: str | None
    location_city: str | None


async def load_offers(
    db: AsyncSession, *, accepted_only: bool = True, ids: Sequence[uuid.UUID] | None = None
) -> dict[uuid.UUID, OfferFact]:
    from app.models.offers import Offer, OfferStatusHistory, OfferVersion

    stmt = select(Offer)
    if accepted_only:
        stmt = stmt.where(
            Offer.id.in_(
                select(OfferStatusHistory.offer_id).where(OfferStatusHistory.to_status == "accepted")
            )
        )
    if ids is not None:
        stmt = stmt.where(Offer.id.in_(list(ids)))
    offers = list((await db.execute(stmt)).scalars())
    if not offers:
        return {}
    oids = [o.id for o in offers]
    accepted: dict[uuid.UUID, datetime] = {}
    for offer_id, at in await db.execute(
        select(OfferStatusHistory.offer_id, func.min(OfferStatusHistory.at))
        .where(OfferStatusHistory.offer_id.in_(oids), OfferStatusHistory.to_status == "accepted")
        .group_by(OfferStatusHistory.offer_id)
    ):
        accepted[offer_id] = to_dt(at)  # type: ignore[assignment]
    versions: dict[uuid.UUID, list[OfferVersion]] = defaultdict(list)
    for v in (
        await db.execute(
            select(OfferVersion).where(OfferVersion.offer_id.in_(oids)).order_by(OfferVersion.version_no)
        )
    ).scalars():
        versions[v.offer_id].append(v)
    out: dict[uuid.UUID, OfferFact] = {}
    for o in offers:
        vs = versions.get(o.id, [])
        current = next(
            (v for v in vs if v.id == (o.released_version_id or o.current_version_id)), vs[-1] if vs else None
        )
        annual = None
        if current is not None and current.pay_rate is not None:
            annual = float(current.pay_rate) * ANNUAL_FACTOR.get(current.pay_rate_unit, 1.0)
        out[o.id] = OfferFact(
            offer_id=o.id,
            candidate_id=o.candidate_id,
            client_id=o.client_id,
            status=o.status,
            accepted_at=accepted.get(o.id) or to_dt(o.responded_at),
            version_times=tuple(to_dt(v.created_at) for v in vs),  # type: ignore[misc]
            start_date=current.start_date if current is not None else None,
            annual_pay=annual,
            pay_currency=current.pay_currency if current is not None else None,
            location_city=city_of(current.location) if current is not None else None,
        )
    return out


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateFact:
    notice_period_days: int | None
    annual_ctc: float | None
    ctc_currency: str | None
    city: str | None


async def load_candidates(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, CandidateFact]:
    """Only notice period, the current CTC (aggregated into a % change, shown as a band) and the city token."""
    from app.models.candidates import Candidate

    cids = {i for i in ids if i is not None}
    if not cids:
        return {}
    rows = await db.execute(
        select(
            Candidate.id,
            Candidate.notice_period_days,
            Candidate.current_ctc_amount,
            Candidate.current_ctc_unit,
            Candidate.current_ctc_currency,
            Candidate.location["city"].astext,
        ).where(Candidate.id.in_(cids))
    )
    out: dict[uuid.UUID, CandidateFact] = {}
    for cid, notice, ctc, unit, currency, city in rows:
        annual = float(ctc) * CTC_FACTOR.get(unit or "annual", 1.0) if ctc is not None else None
        out[cid] = CandidateFact(notice, annual, currency, city_of(city))
    return out


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementFact:
    id: uuid.UUID
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    offer_id: uuid.UUID | None
    status: str
    drop_reason: str | None
    planned_start_date: date
    planned_end_date: date | None
    actual_start_date: date | None
    end_date: date | None
    created_at: datetime
    counter_offer_at: datetime | None
    work_city: str | None
    status_times: dict[str, datetime]


async def load_placements(
    db: AsyncSession, ids: Sequence[uuid.UUID] | None = None, *, statuses: Sequence[str] | None = None
) -> list[PlacementFact]:
    from app.models.placements import Placement, PlacementStatusHistory

    stmt = select(Placement)
    if ids is not None:
        stmt = stmt.where(Placement.id.in_(list(ids)))
    if statuses is not None:
        stmt = stmt.where(Placement.status.in_(list(statuses)))
    rows = list((await db.execute(stmt)).scalars())
    if not rows:
        return []
    times: dict[uuid.UUID, dict[str, datetime]] = defaultdict(dict)
    for pid, to_status, at in await db.execute(
        select(
            PlacementStatusHistory.placement_id,
            PlacementStatusHistory.to_status,
            func.min(PlacementStatusHistory.at),
        )
        .where(PlacementStatusHistory.placement_id.in_([p.id for p in rows]))
        .group_by(PlacementStatusHistory.placement_id, PlacementStatusHistory.to_status)
    ):
        times[pid][to_status] = to_dt(at)  # type: ignore[assignment]
    return [
        PlacementFact(
            id=p.id,
            candidate_id=p.candidate_id,
            client_id=p.client_id,
            offer_id=p.offer_id,
            status=p.status,
            drop_reason=p.drop_reason,
            planned_start_date=p.planned_start_date,
            planned_end_date=p.planned_end_date,
            actual_start_date=p.actual_start_date,
            end_date=p.end_date,
            created_at=to_dt(p.created_at),  # type: ignore[arg-type]
            counter_offer_at=to_dt(p.counter_offer_at),
            work_city=city_of(p.work_location_text),
            status_times=dict(times.get(p.id, {})),
        )
        for p in rows
    ]


async def offers_with_placement(db: AsyncSession, offer_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    from app.models.placements import Placement

    if not offer_ids:
        return set()
    rows = await db.execute(select(Placement.offer_id).where(Placement.offer_id.in_(list(offer_ids))))
    return {r[0] for r in rows if r[0] is not None}


async def onboarding_tasks(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[datetime, datetime | None]]]:
    """Placement id -> [(task created_at, decided_at when approved / waived)]."""
    from app.models.onboarding import OnboardingCase, OnboardingTask

    pids = list({p for p in placement_ids})
    if not pids:
        return {}
    out: dict[uuid.UUID, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    for pid, created_at, status, decided_at in await db.execute(
        select(
            OnboardingCase.placement_id,
            OnboardingTask.created_at,
            OnboardingTask.status,
            OnboardingTask.decided_at,
        )
        .join(OnboardingTask, OnboardingTask.case_id == OnboardingCase.id)
        .where(OnboardingCase.placement_id.in_(pids), OnboardingTask.mandatory.is_(True))
    ):
        done = to_dt(decided_at) if status in ("approved", "waived") else None
        out[pid].append((to_dt(created_at), done))  # type: ignore[arg-type]
    return out


async def bgv_cases(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[datetime, datetime | None]]]:
    """Placement id -> [(case created_at, resolved_at)] (resolved = adjudicated / terminal)."""
    from app.models.bgv import BgvCase

    pids = list({p for p in placement_ids})
    if not pids:
        return {}
    out: dict[uuid.UUID, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    for pid, created_at, status, adjudicated_at, updated_at in await db.execute(
        select(
            BgvCase.placement_id,
            BgvCase.created_at,
            BgvCase.status,
            BgvCase.adjudicated_at,
            BgvCase.updated_at,
        ).where(BgvCase.placement_id.in_(pids), BgvCase.required.is_(True))
    ):
        resolved = to_dt(adjudicated_at) or (
            to_dt(updated_at) if status in ("clear", "failed", "cancelled") else None
        )
        out[pid].append((to_dt(created_at), resolved))  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------- attrition facts
async def timesheet_rejections(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[date]]:
    from app.models.time import Timesheet

    pids = list(set(placement_ids))
    if not pids:
        return {}
    out: dict[uuid.UUID, list[date]] = defaultdict(list)
    for pid, period_end in await db.execute(
        select(Timesheet.placement_id, Timesheet.period_end).where(
            Timesheet.placement_id.in_(pids), Timesheet.status == "rejected"
        )
    ):
        out[pid].append(period_end)
    return out


async def bench_entries(
    db: AsyncSession, candidate_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[datetime]]:
    from app.models.placements import BenchEntry

    cids = list(set(candidate_ids))
    if not cids:
        return {}
    out: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for cid, created_at in await db.execute(
        select(BenchEntry.candidate_id, BenchEntry.created_at).where(BenchEntry.candidate_id.in_(cids))
    ):
        out[cid].append(to_dt(created_at))  # type: ignore[arg-type]
    return out


async def pay_rates(
    db: AsyncSession,
) -> tuple[dict[uuid.UUID, tuple[float, tuple[str, str]]], dict[tuple[str, str], float]]:
    """(placement id -> (latest active pay rate, (currency, unit)), (currency, unit) -> tenant median)."""
    from app.models.placements import RateSegment

    latest: dict[uuid.UUID, tuple[date, float, tuple[str, str]]] = {}
    for pid, eff, rate, currency, unit in await db.execute(
        select(
            RateSegment.placement_id,
            RateSegment.effective_from,
            RateSegment.pay_rate,
            RateSegment.currency,
            RateSegment.rate_unit,
        ).where(RateSegment.status == "active", RateSegment.pay_rate.is_not(None))
    ):
        prev = latest.get(pid)
        if prev is None or eff > prev[0]:
            latest[pid] = (eff, float(rate), (currency, unit))
    by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
    for _eff, rate, key in latest.values():
        by_key[key].append(rate)
    medians = {k: m for k, v in by_key.items() if (m := median(v))}
    return {pid: (rate, key) for pid, (_eff, rate, key) in latest.items()}, medians


async def leave_requests(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[date, float, str, datetime, datetime | None]]]:
    """Placement id -> [(start_date, days, status, created_at, decided_at)]."""
    from app.models.time import LeaveRequest

    pids = list(set(placement_ids))
    if not pids:
        return {}
    out: dict[uuid.UUID, list[tuple[date, float, str, datetime, datetime | None]]] = defaultdict(list)
    for pid, start, days, status, created_at, decided_at in await db.execute(
        select(
            LeaveRequest.placement_id,
            LeaveRequest.start_date,
            LeaveRequest.days,
            LeaveRequest.status,
            LeaveRequest.created_at,
            LeaveRequest.decided_at,
        ).where(LeaveRequest.placement_id.in_(pids))
    ):
        out[pid].append((start, float(days), status, to_dt(created_at), to_dt(decided_at)))  # type: ignore[arg-type]
    return out


async def extensions(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[tuple[str, datetime]]]:
    """Placement id -> [(status, decided_at)] for confirmed / declined extensions."""
    from app.models.placements_exit import Extension

    stmt = select(
        Extension.placement_id,
        Extension.status,
        Extension.confirmed_at,
        Extension.client_response_at,
        Extension.updated_at,
    ).where(Extension.status.in_(("confirmed", "declined")))
    if placement_ids is not None:
        stmt = stmt.where(Extension.placement_id.in_(list(set(placement_ids))))
    out: dict[uuid.UUID, list[tuple[str, datetime]]] = defaultdict(list)
    for pid, status, confirmed_at, responded_at, updated_at in await db.execute(stmt):
        when = to_dt(confirmed_at) or to_dt(responded_at) or to_dt(updated_at)
        out[pid].append((status, when))  # type: ignore[arg-type]
    return out


async def offboarding_reasons(db: AsyncSession) -> dict[uuid.UUID, tuple[str, datetime | None]]:
    """Placement id -> (reason, closed_at) of its latest non-cancelled offboarding case."""
    from app.models.placements_exit import OffboardingCase

    out: dict[uuid.UUID, tuple[str, datetime | None]] = {}
    for pid, reason, closed_at in await db.execute(
        select(OffboardingCase.placement_id, OffboardingCase.reason, OffboardingCase.closed_at)
        .where(OffboardingCase.status != "cancelled")
        .order_by(OffboardingCase.created_at)
    ):
        out[pid] = (reason, to_dt(closed_at))
    return out


async def client_placement_ends(db: AsyncSession) -> dict[uuid.UUID, list[tuple[date, bool]]]:
    """Client id -> [(end_date, ended early)] for ended Placements (client's historical early-end rate)."""
    from app.models.placements import Placement

    out: dict[uuid.UUID, list[tuple[date, bool]]] = defaultdict(list)
    for client_id, end, planned_end in await db.execute(
        select(Placement.client_id, Placement.end_date, Placement.planned_end_date).where(
            and_(Placement.end_date.is_not(None), Placement.status.in_(("ended", "offboarding")))
        )
    ):
        early = planned_end is not None and (planned_end - end).days >= 7
        out[client_id].append((end, early))
    return out
