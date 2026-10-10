"""Matching API service (Epic 5, stories 5.4/5.5): match lists, funnel, recompute, dismiss/restore,
add-to-pipeline. Every demand-scoped call goes through ``demands.matching_port.get_demand`` (404 when the
demand is outside the caller's scope or tenant). Compensation evidence is masked for principals without
``compensation:view``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.permissions import Principal, can_view_compensation
from app.events.matching import MatchAddedToPipeline, MatchDismissed, MatchRestored
from app.models.matching import Match, MatchRun
from app.models.search import CandidateSearchDoc
from app.schemas.matching import (
    DismissIn,
    EvidenceOut,
    FunnelOut,
    FunnelResponse,
    MatchCandidateOut,
    MatchListOut,
    MatchOut,
    RecomputeOut,
    RunOut,
)
from app.services.demands import matching_port as demands
from app.services.matching import runs
from app.services.matching.ports import pipeline_gateway, suppressed_pairs

WRITE = "match:write"


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal):  # pragma: no cover - routes authenticate
        raise NotFound()
    return principal


def _now() -> datetime:
    return datetime.now(UTC)


def not_found() -> NotFound:
    return NotFound("Match not found")


def _evidence(items: list[dict[str, Any]], show_comp: bool) -> list[EvidenceOut]:
    out = []
    for e in items or []:
        comp = bool(e.get("comp"))
        out.append(
            EvidenceOut(
                id=str(e.get("id")),
                kind=str(e.get("kind")),
                text=e.get("text") if show_comp or not comp else None,
                comp=comp,
            )
        )
    return out


def _sub_scores(raw: dict[str, Any], show_comp: bool) -> dict[str, int | None]:
    out = {k: (int(v) if v is not None else None) for k, v in (raw or {}).items()}
    if not show_comp and "compensation" in out:
        out["compensation"] = None
    return out


def _run_out(run: MatchRun | None) -> RunOut | None:
    if run is None:
        return None
    return RunOut(
        id=run.id,
        status=run.status,  # type: ignore[arg-type]
        trigger=run.trigger,
        started_at=run.started_at,
        finished_at=run.finished_at,
        considered=run.considered,
    )


async def _last_runs(db: AsyncSession, demand_id: uuid.UUID) -> tuple[MatchRun | None, MatchRun | None]:
    last = (
        await db.execute(
            select(MatchRun)
            .where(MatchRun.demand_id == demand_id)
            .order_by(MatchRun.started_at.desc(), MatchRun.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    completed = (
        await db.execute(
            select(MatchRun)
            .where(MatchRun.demand_id == demand_id, MatchRun.status == "completed")
            .order_by(MatchRun.finished_at.desc(), MatchRun.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return last, completed


def _funnel(run: MatchRun | None) -> FunnelOut | None:
    if run is None or not run.funnel:
        return None
    return FunnelOut(
        **{
            k: run.funnel.get(k)
            for k in ("total", "relevant", "strong", "available", "strong_threshold")
            if k in run.funnel
        }
    )


async def _hide_suppressed(db: AsyncSession, items: list[MatchOut]) -> list[MatchOut]:
    """Epic 49 (additive): blank the AEDT outputs of pairs a registered score suppressor hides."""
    hidden = await suppressed_pairs(db, [(i.candidate.id, i.demand_id) for i in items])
    for item in items:
        reason = hidden.get((item.candidate.id, item.demand_id))
        if reason is not None:
            item.overall = None
            item.sub_scores = {}
            item.evidence = []
            item.explanation = None
            item.score_hidden_reason = reason
    return items


def _match_out(m: Match, doc: CandidateSearchDoc | None, show_comp: bool, rank: int | None) -> MatchOut:
    return MatchOut(
        id=m.id,
        demand_id=m.demand_id,
        rank=rank,
        candidate=MatchCandidateOut(
            id=m.candidate_id,
            human_id=doc.human_id if doc else None,
            display_name=doc.display_name if doc else None,
            current_title=doc.current_title if doc else None,
            current_employer=doc.current_employer if doc else None,
            city=doc.city if doc else None,
        ),
        overall=float(m.overall),
        sub_scores=_sub_scores(m.sub_scores, show_comp),
        missing_mandatory=list(m.missing_mandatory or []),
        evidence=_evidence(m.evidence, show_comp),
        explanation=m.explanation,
        explanation_status=m.explanation_status,  # type: ignore[arg-type]
        computed_at=m.computed_at,
        dismissed_at=m.dismissed_at,
        dismissed_by=m.dismissed_by,
        dismiss_reason=m.dismiss_reason,  # type: ignore[arg-type]
        dismiss_note=m.dismiss_note,
        added_to_pipeline_at=m.added_to_pipeline_at,
        application_id=m.application_id,
        is_stale=m.is_stale,
    )


async def list_matches(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    include_dismissed: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> MatchListOut:
    principal = _principal(ctx)
    await demands.get_demand(ctx, db, demand_id)
    show_comp = can_view_compensation(principal)
    stmt = (
        select(Match, CandidateSearchDoc)
        .outerjoin(CandidateSearchDoc, CandidateSearchDoc.candidate_id == Match.candidate_id)
        .where(Match.demand_id == demand_id)
    )
    if not include_dismissed:
        stmt = stmt.where(Match.dismissed_at.is_(None))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(Match.dismissed_at.is_not(None), Match.overall.desc(), Match.candidate_id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    last, completed = await _last_runs(db, demand_id)
    offset = (page - 1) * page_size
    items = await _hide_suppressed(
        db, [_match_out(m, doc, show_comp, offset + i + 1) for i, (m, doc) in enumerate(rows)]
    )
    sections = None
    if page == 1:  # Epic 41 (additive): registered sections (stored scores), rendered above the list
        from app.services.matching.sections import build_sections
        from app.services.matching.sections import sections as section_defs

        if section_defs():
            sections = await build_sections(ctx, db, demand_id)
    return MatchListOut(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        can_add_to_pipeline=pipeline_gateway().is_available() and principal.has(WRITE),
        last_run=_run_out(last),
        funnel=_funnel(completed),
        sections=sections,
    )


async def get_funnel(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> FunnelResponse:
    await demands.get_demand(ctx, db, demand_id)
    last, completed = await _last_runs(db, demand_id)
    return FunnelResponse(demand_id=demand_id, funnel=_funnel(completed), last_run=_run_out(last))


async def request_recompute(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> RecomputeOut:
    demand = await demands.get_demand(ctx, db, demand_id)
    if demand.status != "open":
        raise ConflictError("Matches are computed for open demands only", code="demand_not_open")
    run = await runs.request_run(db, demand.tenant_id, demand_id, "manual")
    return RecomputeOut(demand_id=demand_id, status="queued", run_id=run.id)


async def _get_match(
    ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID, *, for_update: bool = True
) -> Match:
    stmt = select(Match).where(Match.id == match_id)
    if for_update:
        stmt = stmt.with_for_update()
    match = (await db.execute(stmt)).scalar_one_or_none()
    if match is None:
        raise not_found()
    try:
        await demands.get_demand(ctx, db, match.demand_id)
    except NotFound as exc:
        raise not_found() from exc
    return match


async def _one(ctx: RequestContext, db: AsyncSession, match: Match) -> MatchOut:
    doc = await db.get(CandidateSearchDoc, match.candidate_id)
    out = _match_out(match, doc, can_view_compensation(_principal(ctx)), None)
    return (await _hide_suppressed(db, [out]))[0]


async def dismiss(ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID, body: DismissIn) -> MatchOut:
    principal = _principal(ctx)
    match = await _get_match(ctx, db, match_id)
    match.dismissed_at = match.dismissed_at or _now()
    match.dismissed_by = principal.id
    match.dismiss_reason = body.reason
    match.dismiss_note = body.note or None
    await db.flush()
    emit(
        db,
        MatchDismissed(
            tenant_id=match.tenant_id,
            match_id=match.id,
            demand_id=match.demand_id,
            candidate_id=match.candidate_id,
            reason=body.reason,
        ),
    )
    return await _one(ctx, db, match)


async def restore(ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID) -> MatchOut:
    match = await _get_match(ctx, db, match_id)
    if match.dismissed_at is not None:
        match.dismissed_at = None
        match.dismissed_by = None
        match.dismiss_reason = None
        match.dismiss_note = None
        await db.flush()
        emit(
            db,
            MatchRestored(
                tenant_id=match.tenant_id,
                match_id=match.id,
                demand_id=match.demand_id,
                candidate_id=match.candidate_id,
            ),
        )
    return await _one(ctx, db, match)


async def add_to_pipeline(ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID) -> MatchOut:
    match = await _get_match(ctx, db, match_id)
    gateway = pipeline_gateway()
    if match.application_id is not None:
        return await _one(ctx, db, match)
    if not gateway.is_available():
        raise ConflictError("The pipeline is not available yet", code="pipeline_unavailable")
    application_id = await gateway.add_candidate(
        ctx, db, match.demand_id, match.candidate_id, source="ai_match", match_id=match.id
    )
    match.application_id = application_id
    match.added_to_pipeline_at = _now()
    await db.flush()
    emit(
        db,
        MatchAddedToPipeline(
            tenant_id=match.tenant_id,
            match_id=match.id,
            demand_id=match.demand_id,
            candidate_id=match.candidate_id,
            application_id=application_id,
        ),
    )
    return await _one(ctx, db, match)


async def scores_for_candidates(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, candidate_ids: list[uuid.UUID]
) -> list[MatchOut]:
    """Epic 24 (additive, read-only): the stored ``matches`` rows of ``candidate_ids`` for one demand, dismissed
    ones included (``dismissed_at`` set). Scores are never recomputed here (AD-8)."""
    if not candidate_ids:
        return []
    principal = _principal(ctx)
    show_comp = can_view_compensation(principal)
    rows = (
        await db.execute(
            select(Match, CandidateSearchDoc)
            .outerjoin(CandidateSearchDoc, CandidateSearchDoc.candidate_id == Match.candidate_id)
            .where(Match.demand_id == demand_id, Match.candidate_id.in_(list(set(candidate_ids))))
            .order_by(Match.overall.desc(), Match.candidate_id)
        )
    ).all()
    return await _hide_suppressed(db, [_match_out(m, doc, show_comp, None) for m, doc in rows])
