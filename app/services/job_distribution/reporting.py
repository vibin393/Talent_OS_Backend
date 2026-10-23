# ruff: file-ignore[line-too-long]
"""Board reporting (Epic 70, story 70.5; spec A6): per board and per Board Posting, within the caller's Demand read
scope -- postings, applications (``source='job_board' AND source_ref=<board posting id>``; careers applications that
carried ``src=<board>&bp=<id>`` are attributed the same way by the apply source resolvers), submissions, interviews,
offers and placements (joined on the application ids), and the free-text cost note. The P3 report builder has no
dataset registry hook for this shape, so this is an API + page (recorded)."""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demands import Demand
from app.models.interviews import Interview
from app.models.job_distribution import BoardPosting
from app.models.offers import Offer
from app.models.pipeline import Application, Submission
from app.models.placements import Placement
from app.schemas.job_distribution import BoardPostingReportRow, BoardReportRow, ReportQuery
from app.services.job_distribution.common import board_label


async def _counts(db: AsyncSession, model: type, app_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not app_ids:
        return {}
    col = model.application_id  # type: ignore[attr-defined]
    rows = (await db.execute(select(col, func.count(func.distinct(col))).where(col.in_(app_ids)).group_by(col))).all()
    return {a: int(n) for a, n in rows}


async def board_performance(ctx: RequestContext, db: AsyncSession, q: ReportQuery) -> list[BoardReportRow]:
    from app.services.demands.common import READ, access_condition, principal_of

    stmt = select(BoardPosting, Demand.title).join(Demand, Demand.id == BoardPosting.demand_id)
    cond = access_condition(principal_of(ctx), READ)
    if cond is not None:
        stmt = stmt.where(cond)
    if q.board_key:
        stmt = stmt.where(BoardPosting.board_key == q.board_key)
    if q.demand_id:
        stmt = stmt.where(BoardPosting.demand_id == q.demand_id)
    if q.from_:
        stmt = stmt.where(BoardPosting.created_at >= datetime.combine(q.from_, time.min, tzinfo=UTC))
    if q.to:
        stmt = stmt.where(BoardPosting.created_at < datetime.combine(q.to + timedelta(days=1), time.min, tzinfo=UTC))
    rows = (await db.execute(stmt.order_by(BoardPosting.board_key, BoardPosting.created_at))).all()
    if not rows:
        return []
    refs = [str(bp.id) for bp, _ in rows]
    apps = (await db.execute(select(Application.id, Application.source_ref).where(Application.source == "job_board", Application.source_ref.in_(refs)))).all()
    by_bp: dict[str, list[uuid.UUID]] = defaultdict(list)
    for app_id, ref in apps:
        by_bp[str(ref)].append(app_id)
    all_ids = [a for a, _ in apps]
    subs = await _counts(db, Submission, all_ids)
    ints = await _counts(db, Interview, all_ids)
    offs = await _counts(db, Offer, all_ids)
    plcs = await _counts(db, Placement, all_ids)

    def _n(counts: dict[uuid.UUID, int], ids: list[uuid.UUID]) -> int:
        return sum(1 for i in ids if counts.get(i))

    boards: dict[str, BoardReportRow] = {}
    for bp, title in rows:
        ids = by_bp.get(str(bp.id), [])
        line = BoardPostingReportRow(
            board_posting_id=bp.id, human_id=bp.human_id, demand_id=bp.demand_id, title=title, status=bp.status,
            applications=len(ids), submissions=_n(subs, ids), interviews=_n(ints, ids), offers=_n(offs, ids), placements=_n(plcs, ids), cost_note=bp.cost_note,
        )  # fmt: skip
        agg = boards.get(bp.board_key)
        if agg is None:
            agg = boards[bp.board_key] = BoardReportRow(board_key=bp.board_key, label=board_label(bp.board_key), postings=0, applications=0, submissions=0, interviews=0, offers=0, placements=0, cost_note=None, rows=[])
        agg.postings += 1
        agg.applications += line.applications
        agg.submissions += line.submissions
        agg.interviews += line.interviews
        agg.offers += line.offers
        agg.placements += line.placements
        notes = [n for n in (agg.cost_note, bp.cost_note) if n]
        agg.cost_note = "; ".join(dict.fromkeys(notes))[:500] or None
        agg.rows.append(line)
    return list(boards.values())
