# ruff: noqa: E501
"""Resource timeline (Epic 45, FR-229): role gaps and Worker over-allocation over a date window.

The core (``role_segments`` / ``worker_segments``) is pure: it cuts the window into maximal intervals with a
constant assignment set. A role interval is a *gap* when the assigned FTE (sum of ``allocation_pct / 100``) is
below the role headcount; a Worker interval is *over-allocated* when the sum of allocations across **all** their
assignments (any project) exceeds 100 %. ``load_timeline`` reads the rows for one project.
"""

import dataclasses
import itertools
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.projects import Project, ProjectAssignment, ProjectRole
from app.schemas.projects import (
    AllocationSplit,
    RoleSegment,
    RoleTimeline,
    TimelineOut,
    WorkerSegment,
    WorkerTimeline,
)

HUNDRED = Decimal(100)
MAX_WINDOW_DAYS = 731
DEFAULT_WINDOW_DAYS = 90


@dataclasses.dataclass(frozen=True, slots=True)
class Span:
    start: date
    end: date | None  # inclusive; None = open-ended
    allocation_pct: Decimal
    project_id: uuid.UUID | None = None
    project_name: str = ""


def _clip(span: Span, lo: date, hi: date) -> tuple[date, date] | None:
    start = max(span.start, lo)
    end = min(span.end or hi, hi)
    return (start, end) if start <= end else None


def _intervals(spans: Sequence[Span], lo: date, hi: date) -> list[tuple[date, date, list[Span]]]:
    """Maximal [start, end] intervals of the window with a constant set of covering spans."""
    cuts = {lo, hi + timedelta(days=1)}
    for s in spans:
        clipped = _clip(s, lo, hi)
        if clipped:
            cuts.add(clipped[0])
            cuts.add(clipped[1] + timedelta(days=1))
    points = sorted(cuts)
    out: list[tuple[date, date, list[Span]]] = []
    for a, b in itertools.pairwise(points):
        end = b - timedelta(days=1)
        covering = [s for s in spans if s.start <= a and (s.end is None or s.end >= a)]
        out.append((a, end, covering))
    return out


def role_segments(
    lo: date,
    hi: date,
    headcount: int,
    spans: Sequence[Span],
    role_start: date | None = None,
    role_end: date | None = None,
) -> list[RoleSegment]:
    """Daily assigned FTE vs headcount, merged into segments (only inside the role's own dates)."""
    lo2 = max(lo, role_start) if role_start else lo
    hi2 = min(hi, role_end) if role_end else hi
    if lo2 > hi2:
        return []
    segments: list[RoleSegment] = []
    for a, b, covering in _intervals(spans, lo2, hi2):
        fte = sum((s.allocation_pct / HUNDRED for s in covering), Decimal(0)).quantize(Decimal("0.01"))
        gap = fte < Decimal(headcount)
        if segments and segments[-1].assigned_fte == fte and segments[-1].end + timedelta(days=1) == a:
            segments[-1] = segments[-1].model_copy(update={"end": b})
            continue
        segments.append(RoleSegment(start=a, end=b, assigned_fte=fte, headcount=headcount, gap=gap))
    return segments


def worker_segments(lo: date, hi: date, spans: Sequence[Span]) -> list[WorkerSegment]:
    """Sum of a Worker's allocations across every assignment; ``over`` above 100 %."""
    segments: list[WorkerSegment] = []
    for a, b, covering in _intervals(spans, lo, hi):
        if not covering:
            continue
        total = sum((s.allocation_pct for s in covering), Decimal(0))
        split = [
            AllocationSplit(
                project_id=s.project_id or uuid.UUID(int=0),
                project_name=s.project_name,
                allocation_pct=s.allocation_pct,
            )
            for s in covering
        ]
        prev = segments[-1] if segments else None
        if prev and prev.total_pct == total and prev.end + timedelta(days=1) == a and prev.split == split:
            segments[-1] = prev.model_copy(update={"end": b})
            continue
        segments.append(WorkerSegment(start=a, end=b, total_pct=total, over=total > HUNDRED, split=split))
    return segments


def window(start: date | None, end: date | None, default_from: date) -> tuple[date, date]:
    lo = start or default_from
    hi = end or (lo + timedelta(days=DEFAULT_WINDOW_DAYS))
    if hi < lo:
        lo, hi = hi, lo
    if (hi - lo).days > MAX_WINDOW_DAYS:
        hi = lo + timedelta(days=MAX_WINDOW_DAYS)
    return lo, hi


def _overlaps(lo: date, hi: date) -> object:
    return (ProjectAssignment.start_date <= hi) & or_(
        ProjectAssignment.end_date.is_(None), ProjectAssignment.end_date >= lo
    )


async def load_timeline(
    db: AsyncSession, project: Project, lo: date, hi: date, names: dict[uuid.UUID, str] | None = None
) -> TimelineOut:
    roles = list(
        (
            await db.execute(
                select(ProjectRole)
                .where(ProjectRole.project_id == project.id, ProjectRole.status != "cancelled")
                .order_by(ProjectRole.start_date.nulls_last(), ProjectRole.title)
            )
        ).scalars()
    )
    mine = list(
        (
            await db.execute(
                select(ProjectAssignment).where(ProjectAssignment.project_id == project.id, _overlaps(lo, hi))
            )
        ).scalars()
    )
    by_role: dict[uuid.UUID, list[Span]] = defaultdict(list)
    for a in mine:
        by_role[a.project_role_id].append(Span(a.start_date, a.end_date, a.allocation_pct))
    role_rows = [
        RoleTimeline(
            project_role_id=r.id,
            title=r.title,
            headcount=r.headcount,
            segments=role_segments(lo, hi, r.headcount, by_role.get(r.id, []), r.start_date, r.end_date),
        )
        for r in roles
    ]
    placement_ids = {a.placement_id for a in mine}
    workers = await _worker_rows(db, placement_ids, lo, hi, names or {})
    return TimelineOut(
        from_date=lo,
        to_date=hi,
        roles=role_rows,
        workers=workers,
        gap_count=sum(1 for r in role_rows for s in r.segments if s.gap),
        over_allocation_count=sum(1 for w in workers for s in w.segments if s.over),
    )


async def _worker_rows(
    db: AsyncSession, placement_ids: Iterable[uuid.UUID], lo: date, hi: date, names: dict[uuid.UUID, str]
) -> list[WorkerTimeline]:
    ids = set(placement_ids)
    if not ids:
        return []
    from app.models.placements import Placement

    cand = dict(
        (await db.execute(select(Placement.id, Placement.candidate_id).where(Placement.id.in_(ids)))).all()
    )
    candidates = {c for c in cand.values() if c is not None}
    # Every assignment of these Workers (any project, any of their placements) inside the window.
    rows = (
        await db.execute(
            select(ProjectAssignment, Project.name)
            .join(Project, Project.id == ProjectAssignment.project_id)
            .join(Placement, Placement.id == ProjectAssignment.placement_id)
            .where(
                or_(Placement.candidate_id.in_(candidates), ProjectAssignment.placement_id.in_(ids)),
                _overlaps(lo, hi),
            )
        )
    ).all()
    per_worker: dict[uuid.UUID, list[Span]] = defaultdict(list)
    placement_of: dict[uuid.UUID, uuid.UUID] = {}
    for a, project_name in rows:
        key = a.candidate_id or a.placement_id
        per_worker[key].append(Span(a.start_date, a.end_date, a.allocation_pct, a.project_id, project_name))
        if a.placement_id in ids:
            placement_of[key] = a.placement_id
    out: list[WorkerTimeline] = []
    for key, spans in per_worker.items():
        if key not in placement_of:
            continue
        cid = key if key in candidates else None
        out.append(
            WorkerTimeline(
                candidate_id=cid,
                placement_id=placement_of[key],
                worker_name=names.get(cid) if cid else None,
                segments=worker_segments(lo, hi, spans),
            )
        )
    return sorted(out, key=lambda w: (w.worker_name or "", str(w.placement_id)))
