"""Bench-first matching (Epic 24, FR-126).

Ranking uses the stored deterministic Match ``overall`` only (AD-8): the LLM never ranks or filters.
Scores come from the P1 matcher (``matching.service.scores_for_candidates``); bench
candidates without a row are queued for scoring and shown ``score_pending``.

* ``bench_matches``: the Bench section of an Open Demand's Matching tab (bench pool + rolling-off Workers).
* ``notify_strong``: one ``bench_match_notices`` row (and one notification) per (demand, candidate) pair.
* Reviews / dismissals, "bench Matches reviewed" completion and the ``bench.matches_reviewed`` condition.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.models.matching import Match
from app.models.placements import (
    BENCH_OPEN_STATUSES,
    BenchEntry,
    BenchMatchNotice,
    BenchMatchReview,
    BenchMatchReviewCompletion,
    Placement,
)
from app.schemas.bench import (
    BenchMatchCandidate,
    BenchMatchesOut,
    BenchMatchItem,
    ReviewCompleteOut,
    ReviewIn,
    ReviewOut,
)
from app.services.placements import bench, bench_cost
from app.services.placements.bench_events import BenchMatchesReviewed, BenchStrongMatchFound


def _now() -> datetime:
    return datetime.now(UTC)


@dataclasses.dataclass(frozen=True, slots=True)
class PoolMember:
    candidate_id: uuid.UUID
    pool: str  # bench | rolling_off
    entry: BenchEntry | None
    rolling_off_end_date: date | None


async def pool(db: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, PoolMember]:
    """Open bench entries plus Workers whose Active Placement ends within ``upcoming_roll_off_days``."""
    settings = await bench.settings_row(db, tenant_id)
    today = await bench.tenant_today(db, tenant_id)
    out: dict[uuid.UUID, PoolMember] = {}
    for entry in (
        await db.execute(select(BenchEntry).where(BenchEntry.status.in_(BENCH_OPEN_STATUSES)))
    ).scalars():
        out[entry.candidate_id] = PoolMember(entry.candidate_id, "bench", entry, None)
    ends = func.coalesce(Placement.end_date, Placement.planned_end_date)
    horizon = today + timedelta(days=settings.upcoming_roll_off_days)
    for cid, end in await db.execute(
        select(Placement.candidate_id, ends).where(
            Placement.status == "active", ends >= today, ends <= horizon
        )
    ):
        if cid not in out or (
            out[cid].pool == "rolling_off"
            and out[cid].rolling_off_end_date
            and end < out[cid].rolling_off_end_date
        ):
            out[cid] = PoolMember(cid, "rolling_off", None, end)
    return out


async def _strong_threshold(db: AsyncSession) -> int:
    from app.services.matching.settings import effective

    return int((await effective(db)).strong_threshold)


async def _reviews(db: AsyncSession, demand_id: uuid.UUID) -> dict[uuid.UUID, BenchMatchReview]:
    return {
        r.candidate_id: r
        for r in (
            await db.execute(select(BenchMatchReview).where(BenchMatchReview.demand_id == demand_id))
        ).scalars()
    }


async def _completion(db: AsyncSession, demand_id: uuid.UUID) -> BenchMatchReviewCompletion | None:
    return (
        await db.execute(
            select(BenchMatchReviewCompletion).where(BenchMatchReviewCompletion.demand_id == demand_id)
        )
    ).scalar_one_or_none()


async def _strong_rows(
    db: AsyncSession, demand_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID], threshold: int
) -> list[tuple[uuid.UUID, Decimal]]:
    if not candidate_ids:
        return []
    rows = await db.execute(
        select(Match.candidate_id, Match.overall).where(
            Match.demand_id == demand_id,
            Match.candidate_id.in_(list(candidate_ids)),
            Match.dismissed_at.is_(None),
            Match.overall >= threshold,
        )
    )
    return [(r[0], Decimal(str(r[1]))) for r in rows]


async def _open_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services.demands import matching_port as demands

    demand = await demands.get_demand(ctx, db, demand_id)
    if demand.status != "open":
        raise ConflictError("The demand is not open", code="demand_not_open")
    return demand


def _queue_scoring(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    if not ids:
        return
    from app.services.matching.handlers import score_later

    score_later(tenant_id, ids)


async def bench_matches(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> BenchMatchesOut:
    from app.models.candidates import Candidate
    from app.services.candidates.search_port import ai_consent_active
    from app.services.matching.service import scores_for_candidates

    await _open_demand(ctx, db, demand_id)
    tid = bench._tenant(ctx, db)
    members = await pool(db, tid)
    ids = list(members)
    scores = {m.candidate.id: m for m in await scores_for_candidates(ctx, db, demand_id, ids)}
    consent = await ai_consent_active(db, ids)
    threshold = await _strong_threshold(db)
    reviews = await _reviews(db, demand_id)
    completion = await _completion(db, demand_id)
    settings = await bench.settings_row(db, tid)
    today = await bench.tenant_today(db, tid)
    show = bench._show_comp(ctx)
    names = {
        r.id: r
        for r in await db.execute(
            select(
                Candidate.id,
                Candidate.human_id,
                Candidate.full_name,
                Candidate.current_title,
                Candidate.location,
            ).where(Candidate.id.in_(ids or [uuid.uuid4()]))
        )
    }
    pending: list[uuid.UUID] = []
    items: list[BenchMatchItem] = []
    for cid, member in members.items():
        match = scores.get(cid)
        no_consent = cid not in consent
        score_pending = match is None and not no_consent
        if score_pending:
            pending.append(cid)
        entry = member.entry
        days = (
            bench_cost.bench_days(entry.available_from, today, settings.bench_day_basis)
            if entry is not None
            else None
        )
        review = reviews.get(cid)
        cand = names.get(cid)
        loc = cand.location if cand is not None and isinstance(cand.location, dict) else {}
        items.append(
            BenchMatchItem(
                candidate=BenchMatchCandidate(
                    id=cid,
                    human_id=getattr(cand, "human_id", None),
                    display_name=getattr(cand, "full_name", None),
                    current_title=getattr(cand, "current_title", None),
                    city=loc.get("city"),
                ),
                pool=member.pool,
                match_id=match.id if match else None,
                overall=match.overall if match else None,
                sub_scores=match.sub_scores if match else {},
                explanation=match.explanation if match else None,
                dismissed_at=match.dismissed_at if match else None,
                application_id=match.application_id if match else None,
                score_pending=score_pending,
                score_unavailable_reason="no_consent" if no_consent and match is None else None,
                strong=bool(
                    match
                    and match.dismissed_at is None
                    and match.overall is not None
                    and match.overall >= threshold
                ),
                bench_entry_id=entry.id if entry else None,
                bench_status=entry.status if entry else None,
                bench_days=days,
                available_from=entry.available_from if entry else None,
                rolling_off_end_date=member.rolling_off_end_date,
                cost_per_day=entry.cost_per_day if entry is not None and show else None,
                cost_currency=entry.cost_currency if entry is not None else None,
                cost_masked=not show,
                review_decision=review.decision if review else None,
                review_reason=review.reason if review else None,
            )
        )
    items.sort(
        key=lambda i: (
            -(i.overall if i.overall is not None else -1.0),
            -(i.bench_days or 0),
            str(i.candidate.id),
        )
    )
    _queue_scoring(tid, pending)
    unreviewed = sum(1 for i in items if i.strong and i.review_decision is None)
    return BenchMatchesOut(
        demand_id=demand_id,
        strong_threshold=threshold,
        items=items,
        unreviewed_strong=unreviewed,
        reviewed_complete=completion is not None,
        completed_at=completion.completed_at if completion else None,
    )


# ---------------------------------------------------------------- strong-match notices (idempotent)
async def notify_strong(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    demand_ids: Sequence[uuid.UUID] | None = None,
    candidate_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Insert one notice per new (demand, candidate) strong pair and emit ``BenchStrongMatchFound`` for it."""
    from app.services.demands import matching_port as demands

    tid = bench._tenant(ctx, db)
    settings = await bench.settings_row(db, tid)
    if not settings.notify_strong_bench_match:
        return 0
    members = await pool(db, tid)
    ids = [c for c in members if candidate_ids is None or c in set(candidate_ids)]
    if not ids:
        return 0
    open_ids = set(await demands.list_open_demand_ids(db))
    targets = [d for d in (demand_ids if demand_ids is not None else open_ids) if d in open_ids]
    threshold = await _strong_threshold(db)
    created = 0
    for demand_id in targets:
        for cid, overall in await _strong_rows(db, demand_id, ids, threshold):
            inserted = (
                await db.execute(
                    insert(BenchMatchNotice)
                    .values(
                        id=uuid.uuid4(), tenant_id=tid, demand_id=demand_id, candidate_id=cid, overall=overall
                    )
                    .on_conflict_do_nothing(constraint="uq_bench_match_notices_pair")
                    .returning(BenchMatchNotice.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                created += 1
                emit(
                    db,
                    BenchStrongMatchFound(
                        tenant_id=tid, demand_id=demand_id, candidate_id=cid, overall=str(overall)
                    ),
                )
    return created


# ---------------------------------------------------------------- reviews
async def review(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: ReviewIn) -> ReviewOut:
    await _open_demand(ctx, db, demand_id)
    tid = bench._tenant(ctx, db)
    members = await pool(db, tid)
    if data.candidate_id not in members:
        raise ConflictError("The candidate is not in the bench pool", code="not_on_bench")
    user_id = getattr(ctx.principal, "id", None)
    now = _now()
    stmt = insert(BenchMatchReview).values(
        id=uuid.uuid4(),
        tenant_id=tid,
        demand_id=demand_id,
        candidate_id=data.candidate_id,
        decision=data.decision,
        reason=data.reason,
        decided_by=user_id,
        decided_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_bench_match_reviews_pair",
        set_={
            "decision": data.decision,
            "reason": data.reason,
            "decided_by": user_id,
            "decided_at": now,
            "updated_at": now,
        },
    )
    await db.execute(stmt)
    return ReviewOut(
        demand_id=demand_id,
        candidate_id=data.candidate_id,
        decision=data.decision,
        reason=data.reason,
        decided_by=user_id,
        decided_at=now,
    )


async def _unreviewed_strong(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> list[uuid.UUID]:
    members = await pool(db, tenant_id)
    strong = await _strong_rows(db, demand_id, list(members), await _strong_threshold(db))
    reviews = await _reviews(db, demand_id)
    return sorted((cid for cid, _ in strong if cid not in reviews), key=str)


async def complete_review(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> ReviewCompleteOut:
    await _open_demand(ctx, db, demand_id)
    tid = bench._tenant(ctx, db)
    missing = await _unreviewed_strong(db, tid, demand_id)
    if missing:
        raise ConflictError(
            "Review or dismiss every strong bench match first",
            code="bench_matches_unreviewed",
            details={"candidate_ids": [str(c) for c in missing]},
        )
    row = await _completion(db, demand_id)
    if row is None:
        row = BenchMatchReviewCompletion(
            tenant_id=tid,
            demand_id=demand_id,
            completed_by=getattr(ctx.principal, "id", None),
            completed_at=_now(),
        )
        db.add(row)
        await db.flush()
        emit(db, BenchMatchesReviewed(tenant_id=tid, demand_id=demand_id))
    return ReviewCompleteOut(
        demand_id=demand_id, completed_at=row.completed_at, completed_by=row.completed_by
    )


async def matches_reviewed(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> bool:
    """``bench.matches_reviewed``: a completion row exists, or the bench pool has no strong Match."""
    if await _completion(db, demand_id) is not None:
        return True
    return not await _any_strong(db, bench._tenant(ctx, db), demand_id)


async def _any_strong(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> bool:
    members = await pool(db, tenant_id)
    return bool(await _strong_rows(db, demand_id, list(members), await _strong_threshold(db)))
