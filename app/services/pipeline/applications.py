"""Applications (story 6.1, FR-32/FR-33) and ``transition_application`` -- the only writer of
``applications.stage_id`` (AD-4).

``transition_application`` validates the move against ``stages.TRANSITIONS`` and the reason rules, checks
optimistic concurrency (``expected_stage_id``), writes ``application_stage_history``, adds system activities
on the candidate and the demand, emits ``ApplicationStageChanged`` and (reject + setting enabled) requests the
tenant rejection email, all in the caller's transaction.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams
from app.core.sequences import next_human_id
from app.events.pipeline import ApplicationCreated, ApplicationStageChanged, RejectionEmailRequested
from app.models.pipeline import (
    Application,
    ApplicationStageHistory,
    ClientFeedback,
    PipelineStage,
    Screening,
    Submission,
)
from app.schemas.pipeline import (
    ApplicationCreateItem,
    ApplicationDetailOut,
    ApplicationOut,
    ApplicationsCreate,
    ApplicationsCreateOut,
    BoardColumn,
    BoardOut,
    BulkItemOut,
    BulkTransitionIn,
    BulkTransitionOut,
    CandidateApplicationOut,
    CandidateSummary,
    DemandProgressCounts,
    StageHistoryOut,
    StageRef,
    SubmissionBrief,
    TransitionIn,
)
from app.services.pipeline import stages
from app.services.pipeline.common import (
    CREATE,
    MOVE,
    READ,
    REJECT,
    ActorKind,
    acting_as,
    actor_id,
    actor_kind_of,
    conflict,
    demand_for,
    item_savepoint,
    now,
    principal_of,
    settings_row,
    tenant_of,
    timeline,
    visible_applications,
)
from app.services.pipeline.common import application_for as application_for

APPLICATION_SEQUENCE_KEY = "APP"

# Epic 41 (additive registry): guards run inside ``transition_application`` before any move. A guard raises
# ``ConflictError`` to refuse (e.g. mobility holds an internal application at ``applied`` until manager approval);
# no behaviour change when no guard refuses. ``(ctx, db, app, from_stage, to_stage, actor) -> None``.
TransitionGuard = Callable[
    [RequestContext, AsyncSession, Application, PipelineStage, PipelineStage, str], Awaitable[None]
]
_TRANSITION_GUARDS: list[TransitionGuard] = []


def register_transition_guard(fn: TransitionGuard) -> None:
    """Register a transition guard (idempotent per function object)."""
    if fn not in _TRANSITION_GUARDS:
        _TRANSITION_GUARDS.append(fn)


def transition_guards() -> tuple[TransitionGuard, ...]:
    return tuple(_TRANSITION_GUARDS)


def _error(exc: DomainError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, "details": exc.details, "status": exc.status_code}


# ================================================================ transition
async def _resolve_target(db: AsyncSession, to_stage: uuid.UUID | str) -> PipelineStage:
    if isinstance(to_stage, str):
        try:
            to_stage = uuid.UUID(to_stage)
        except ValueError:
            return await stages.stage_by_key(db, to_stage)
    await stages.ensure_default_stages(db)
    row = await db.get(PipelineStage, to_stage)
    if row is None:
        raise NotFound("Stage not found")
    return row


async def _validate_reason(
    db: AsyncSession,
    from_stage: PipelineStage,
    to_stage: PipelineStage,
    reason_code: str | None,
    note: str | None,
) -> None:
    kind = stages.REASON_TARGETS.get(to_stage.key)
    if kind is not None:
        if not reason_code:
            raise ValidationFailed("A reason code is required", code="reason_required")
        if await stages.active_reason(db, kind, reason_code) is None:
            raise ValidationFailed(
                "Unknown reason code", code="invalid_reason_code", details={"reason_code": reason_code}
            )
        if reason_code == "other" and not (note or "").strip():
            raise ValidationFailed("A note is required for reason 'other'", code="note_required")
    elif stages.is_terminal(from_stage.key) and to_stage.key == "applied":
        if not (note or "").strip():
            raise ValidationFailed("Reopening requires a reason", code="reason_required")
        if reason_code and await stages.active_reason(db, "reopen", reason_code) is None:
            raise ValidationFailed(
                "Unknown reason code", code="invalid_reason_code", details={"reason_code": reason_code}
            )


async def transition_application(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    to_stage_id: uuid.UUID | str,
    actor: ActorKind,
    reason_code: str | None = None,
    note: str | None = None,
    expected_stage_id: uuid.UUID | None = None,
    *,
    send_rejection_email: bool = False,
) -> Application:
    """Move an application (``to_stage_id`` may also be a stage key). The only writer of ``stage_id``.

    409: ``transition_not_allowed`` (table / actor), ``use_submission_flow`` (a non-system move to
    Submitted), ``stage_conflict`` (stale ``expected_stage_id``), ``same_stage``, ``stage_archived``.
    422: ``reason_required``, ``invalid_reason_code``, ``note_required``.
    """
    app = (
        await db.execute(select(Application).where(Application.id == application_id).with_for_update())
    ).scalar_one_or_none()
    if app is None:
        raise NotFound("Application not found")
    if expected_stage_id is not None and expected_stage_id != app.stage_id:
        raise conflict(
            "stage_conflict",
            "The application moved in the meantime; reload and try again",
            current_stage_id=str(app.stage_id),
        )
    from_stage = await db.get(PipelineStage, app.stage_id)
    assert from_stage is not None
    to_stage = await _resolve_target(db, to_stage_id)
    if to_stage.id == from_stage.id:
        raise conflict("same_stage", "The application is already in this stage")
    if to_stage.archived_at is not None:
        raise conflict("stage_archived", "That stage is archived")
    for guard in tuple(_TRANSITION_GUARDS):  # Epic 41: registered guards refuse with ConflictError
        await guard(ctx, db, app, from_stage, to_stage, actor)
    allowed = stages.allowed_actors(from_stage.key, from_stage.kind, to_stage.key, to_stage.kind)
    if to_stage.key == "submitted" and actor != "system":
        raise conflict("use_submission_flow", "Use Submit to client to move an application to Submitted")
    if allowed is None or actor not in allowed:
        raise conflict(
            "transition_not_allowed",
            "This stage change is not allowed",
            from_stage=from_stage.key,
            to_stage=to_stage.key,
            actor=actor,
        )
    await _validate_reason(db, from_stage, to_stage, reason_code, note)

    at = now()
    who = actor_id(ctx) if actor in ("user", "api_key", "app") else None
    with acting_as(db, actor if actor != "api_key" else "api_key", who):
        app.stage_id = to_stage.id
        app.stage_entered_at = at
        app.version = app.version + 1
        db.add(
            ApplicationStageHistory(
                id=uuid.uuid4(),
                tenant_id=app.tenant_id,
                application_id=app.id,
                from_stage_id=from_stage.id,
                to_stage_id=to_stage.id,
                actor_kind=actor,
                actor_id=who,
                reason_code=reason_code,
                note=(note or None),
                at=at,
            )
        )
        await db.flush()
    body = f"{app.human_id}: {from_stage.label} -> {to_stage.label}"
    data = {
        "application_id": str(app.id),
        "from": from_stage.key,
        "to": to_stage.key,
        "reason_code": reason_code,
    }
    await timeline(ctx, db, "candidate", app.candidate_id, body, data)
    await timeline(ctx, db, "demand", app.demand_id, body, data)
    emit(
        db,
        ApplicationStageChanged(
            tenant_id=app.tenant_id,
            application_id=app.id,
            demand_id=app.demand_id,
            candidate_id=app.candidate_id,
            from_key=from_stage.key,
            to_key=to_stage.key,
            actor_kind=actor,
            reason_code=reason_code,
            origin_chain_depth=int(getattr(ctx, "workflow_chain_depth", 0) or 0),
        ),
    )
    if to_stage.key == "rejected" and send_rejection_email:
        settings = await settings_row(db)
        if settings.rejection_email_enabled:
            emit(db, RejectionEmailRequested(tenant_id=app.tenant_id, application_id=app.id))
    return app


async def system_move(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, target: PipelineStage, note: str
) -> None:
    """Archive-stage evacuation: move with the ``system`` actor, bypassing the actor table (recorded in the
    stage history). Only used between ``applied`` / custom stages."""
    app = (
        await db.execute(select(Application).where(Application.id == application_id).with_for_update())
    ).scalar_one_or_none()
    if app is None or app.stage_id == target.id:
        return
    at = now()
    with acting_as(db, "system"):
        db.add(
            ApplicationStageHistory(
                id=uuid.uuid4(),
                tenant_id=app.tenant_id,
                application_id=app.id,
                from_stage_id=app.stage_id,
                to_stage_id=target.id,
                actor_kind="system",
                actor_id=None,
                reason_code=None,
                note=note,
                at=at,
            )
        )
        app.stage_id = target.id
        app.stage_entered_at = at
        app.version = app.version + 1
        await db.flush()
    emit(
        db,
        ApplicationStageChanged(
            tenant_id=app.tenant_id,
            application_id=app.id,
            demand_id=app.demand_id,
            candidate_id=app.candidate_id,
            from_key=None,
            to_key=target.key,
            actor_kind="system",
        ),
    )
    del ctx


# ================================================================ create
async def _count_for_demand(db: AsyncSession, demand_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Application)
                .where(Application.demand_id == demand_id, Application.archived_at.is_(None))
            )
        ).scalar_one()
    )


async def _sync_demand_counts(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    from app.services import demands as demands_service

    sent = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Submission)
                .where(Submission.demand_id == demand_id, Submission.status == "sent")
            )
        ).scalar_one()
    )
    await demands_service.record_pipeline_counts(
        ctx, db, demand_id, applications=await _count_for_demand(db, demand_id), submissions=sent
    )


async def _existing(db: AsyncSession, demand_id: uuid.UUID, candidate_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Application.id).where(
                Application.demand_id == demand_id, Application.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def create_application(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    source: str,
    source_ref: str | None,
    actor: ActorKind,
    owner_id: uuid.UUID,
) -> Application:
    """Insert one application at ``applied`` (409 ``application_exists`` with ``details.application_id``)."""
    existing = await _existing(db, demand_id, candidate_id)
    if existing is not None:
        raise conflict(
            "application_exists",
            "This candidate is already in the demand's pipeline",
            application_id=str(existing),
        )
    applied = await stages.stage_by_key(db, "applied")
    allowed = stages.allowed_actors(None, None, "applied", "anchored")
    if allowed is None or actor not in allowed:
        raise conflict("transition_not_allowed", "This actor cannot add applications", actor=actor)
    from app.services import settings_service

    prefix = await settings_service.id_prefix(db, APPLICATION_SEQUENCE_KEY)
    at = now()
    app = Application(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, APPLICATION_SEQUENCE_KEY, prefix, width=6),
        candidate_id=candidate_id,
        demand_id=demand_id,
        stage_id=applied.id,
        stage_entered_at=at,
        source=source,
        source_ref=source_ref,
        owner_id=owner_id,
        screening_passed=False,
        version=1,
    )
    try:
        async with db.begin_nested():
            db.add(app)
            db.add(
                ApplicationStageHistory(
                    id=uuid.uuid4(),
                    tenant_id=app.tenant_id,
                    application_id=app.id,
                    from_stage_id=None,
                    to_stage_id=applied.id,
                    actor_kind=actor,
                    actor_id=actor_id(ctx) if actor == "user" else None,
                    reason_code=None,
                    note=None,
                    at=at,
                )
            )
            await db.flush()
    except IntegrityError as exc:  # concurrent add of the same pair
        existing = await _existing(db, demand_id, candidate_id)
        raise conflict(
            "application_exists",
            "This candidate is already in the demand's pipeline",
            application_id=str(existing) if existing else None,
        ) from exc
    body = f"{app.human_id}: added to pipeline ({source})"
    data = {"application_id": str(app.id), "source": source}
    await timeline(ctx, db, "candidate", candidate_id, body, data)
    await timeline(ctx, db, "demand", demand_id, body, data)
    emit(
        db,
        ApplicationCreated(
            tenant_id=app.tenant_id,
            application_id=app.id,
            demand_id=demand_id,
            candidate_id=candidate_id,
            source=source,
            actor_kind=actor,
            origin_chain_depth=int(getattr(ctx, "workflow_chain_depth", 0) or 0),
        ),
    )
    return app


async def add_candidates(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: ApplicationsCreate
) -> ApplicationsCreateOut:
    """Bulk add (per-item ``created`` / ``exists`` / ``error``). A single duplicate add raises 409."""
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    demand = await demand_for(ctx, db, demand_id, CREATE)
    demands_port.assert_demand_accepts_applications(demand)
    actor = actor_kind_of(ctx)
    owner = actor_id(ctx) if actor == "user" else demand.owner_id
    ids = list(dict.fromkeys(data.candidate_ids))
    items: list[ApplicationCreateItem] = []
    for cid in ids:
        try:
            if not await candidates.exists_visible(ctx, db, cid):
                raise NotFound("Candidate not found")
            app = await create_application(
                ctx,
                db,
                demand.id,
                cid,
                source=data.source,
                source_ref=data.source_ref,
                actor=actor,
                owner_id=owner,
            )
            items.append(ApplicationCreateItem(candidate_id=cid, status="created", application_id=app.id))
        except ConflictError as exc:
            if exc.code != "application_exists":
                raise
            if len(ids) == 1:
                raise
            existing = exc.details.get("application_id")
            items.append(
                ApplicationCreateItem(
                    candidate_id=cid,
                    status="exists",
                    existing_application_id=uuid.UUID(existing) if existing else None,
                )
            )
        except DomainError as exc:
            if len(ids) == 1:
                raise
            items.append(ApplicationCreateItem(candidate_id=cid, status="error", error=_error(exc)))
    if any(i.status == "created" for i in items):
        await _sync_demand_counts(ctx, db, demand.id)
        if actor == "user" and owner is not None:
            from app.services import demands as demands_service

            await demands_service.record_first_application(ctx, db, demand.id, owner)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return ApplicationsCreateOut(items=items, counts=counts)


async def add_candidate(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    candidate_id: uuid.UUID,
    source: str = "match",
    source_ref: str | None = None,
) -> uuid.UUID:
    """Single add for other domains (Epic 5 matches, portals). Idempotent: returns the existing id."""
    from app.services.demands import pipeline_port as demands_port

    demand = await demand_for(ctx, db, demand_id, CREATE)
    demands_port.assert_demand_accepts_applications(demand)
    existing = await _existing(db, demand.id, candidate_id)
    if existing is not None:
        return existing
    actor = actor_kind_of(ctx)
    owner = actor_id(ctx) if actor == "user" else demand.owner_id
    app = await create_application(
        ctx, db, demand.id, candidate_id, source=source, source_ref=source_ref, actor=actor, owner_id=owner
    )
    await _sync_demand_counts(ctx, db, demand.id)
    return app.id


async def candidate_ids_for_demand(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    demand_id: uuid.UUID,
) -> set[uuid.UUID]:
    rows = await db.execute(select(Application.candidate_id).where(Application.demand_id == demand_id))
    return set(rows.scalars())


# ================================================================ read models
async def _match_scores(
    db: AsyncSession, demand_id: uuid.UUID, candidate_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not candidate_ids:
        return {}
    try:
        from app.models.matching import Match
    except ImportError:  # pragma: no cover - Epic 5 absent
        return {}
    try:
        async with db.begin_nested():
            rows = await db.execute(
                select(Match.candidate_id, Match.overall).where(
                    Match.demand_id == demand_id, Match.candidate_id.in_(candidate_ids)
                )
            )
            return dict(rows.all())
    except Exception:  # pragma: no cover - matching tables not migrated yet
        return {}


async def _hidden_scores(db: AsyncSession, apps: list[Application]) -> dict[tuple[uuid.UUID, uuid.UUID], str]:
    """Epic 49 (additive): ``(candidate_id, demand_id) -> reason`` for pairs a registered score suppressor hides."""
    from app.services.matching.ports import suppressed_pairs

    return await suppressed_pairs(db, [(a.candidate_id, a.demand_id) for a in apps])


async def _interview_badges(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    """Epic 7 board badges (next interview, pending scorecards); empty when the interviews domain is absent."""
    try:
        from app.services import interviews_service
    except ImportError:  # pragma: no cover - Epic 7 absent
        return {}
    try:
        async with db.begin_nested():
            return dict(await interviews_service.board_badges(None, db, ids))
    except Exception:
        logger.exception("Interview board badges unavailable")
        return {}


async def build_outs(db: AsyncSession, apps: Iterable[Application]) -> list[ApplicationOut]:
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    rows = list(apps)
    if not rows:
        return []
    smap = await stages.stage_map(db)
    ids = [a.id for a in rows]
    cands = await candidates.summaries(db, [a.candidate_id for a in rows])
    demands = await demands_port.titles(db, list({a.demand_id for a in rows}))
    screening: dict[uuid.UUID, str] = {}
    for app_id, status in (
        await db.execute(
            select(Screening.application_id, Screening.status)
            .where(Screening.application_id.in_(ids))
            .order_by(Screening.created_at)
        )
    ).all():
        screening[app_id] = status
    latest: dict[uuid.UUID, Submission] = {}
    for sub in (
        await db.execute(
            select(Submission)
            .where(Submission.application_id.in_(ids), Submission.status != "cancelled")
            .order_by(Submission.created_at)
        )
    ).scalars():
        latest[sub.application_id] = sub
    pending: dict[uuid.UUID, uuid.UUID] = {}
    for fb in (
        await db.execute(
            select(ClientFeedback)
            .join(Submission, Submission.id == ClientFeedback.submission_id)
            .where(
                ClientFeedback.application_id.in_(ids),
                ClientFeedback.decision == "rejected",
                ClientFeedback.confirmed_at.is_(None),
                ClientFeedback.dismissed_at.is_(None),
                Submission.feedback_status == "rejected_pending",
            )
            .order_by(ClientFeedback.received_at)
        )
    ).scalars():
        pending[fb.application_id] = fb.id
    scores: dict[uuid.UUID, Any] = {}
    by_demand: dict[uuid.UUID, list[uuid.UUID]] = {}
    for a in rows:
        by_demand.setdefault(a.demand_id, []).append(a.candidate_id)
    for demand_id, cids in by_demand.items():
        for cid, score in (await _match_scores(db, demand_id, cids)).items():
            scores[demand_id, cid] = score  # type: ignore[index]
    badges = await _interview_badges(db, ids)
    hidden = await _hidden_scores(db, rows)
    at = now()
    out: list[ApplicationOut] = []
    for a in rows:
        stage = smap[a.stage_id]
        c = cands.get(a.candidate_id, {})
        sub = latest.get(a.id)
        out.append(
            ApplicationOut(
                id=a.id,
                human_id=a.human_id,
                demand_id=a.demand_id,
                demand_title=(demands.get(a.demand_id) or {}).get("title"),
                candidate=CandidateSummary(
                    id=a.candidate_id,
                    human_id=c.get("human_id"),
                    full_name=c.get("full_name"),
                    current_title=c.get("current_title"),
                    current_employer=c.get("current_employer"),
                    city=c.get("city"),
                ),
                stage=StageRef(id=stage.id, key=stage.key, label=stage.label, kind=stage.kind),
                stage_entered_at=a.stage_entered_at,
                days_in_stage=max((at - a.stage_entered_at).days, 0),
                source=a.source,
                source_ref=a.source_ref,
                owner_id=a.owner_id,
                screening_passed=a.screening_passed,
                screening_status=screening.get(a.id),
                last_client_feedback=a.last_client_feedback,
                pending_client_rejection_id=pending.get(a.id),
                latest_submission=SubmissionBrief(
                    id=sub.id,
                    human_id=sub.human_id,
                    status=sub.status,
                    feedback_status=sub.feedback_status,
                    sent_at=sub.sent_at,
                )
                if sub is not None
                else None,
                match_score=None
                if (a.candidate_id, a.demand_id) in hidden
                else scores.get((a.demand_id, a.candidate_id)),  # type: ignore[call-overload]
                score_hidden_reason=hidden.get((a.candidate_id, a.demand_id)),
                next_interview_at=getattr(badges.get(a.id), "next_interview_at", None),
                scorecards_pending=int(getattr(badges.get(a.id), "scorecards_pending", 0) or 0),
                version=a.version,
                created_at=a.created_at,
            )
        )
    return out


def _search(stmt: Any, q: str | None) -> Any:
    if not q or not q.strip():
        return stmt
    from app.models.candidates import Candidate

    term = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    matches = select(Candidate.id).where(
        or_(
            Candidate.full_name.ilike(term),
            Candidate.human_id.ilike(term),
            Candidate.current_title.ilike(term),
        )
    )
    return stmt.where(or_(Application.candidate_id.in_(matches), Application.human_id.ilike(term)))


async def list_for_demand(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    params: PageParams,
    stage_id: uuid.UUID | None = None,
    q: str | None = None,
) -> Page[ApplicationOut]:
    demand = await demand_for(ctx, db, demand_id, READ)
    stmt = select(Application).where(Application.demand_id == demand.id, Application.archived_at.is_(None))
    if stage_id is not None:
        stmt = stmt.where(Application.stage_id == stage_id)
    stmt = _search(stmt, q).order_by(Application.stage_entered_at.desc(), Application.id)
    total = int(
        (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    )
    rows = list((await db.execute(stmt.limit(params.page_size).offset(params.offset))).scalars())
    return Page[ApplicationOut](
        items=await build_outs(db, rows), total=total, page=params.page, page_size=params.page_size
    )


async def board(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, q: str | None = None
) -> BoardOut:
    demand = await demand_for(ctx, db, demand_id, READ)
    all_rows = await stages.all_stages(db)
    stmt = _search(
        select(Application).where(Application.demand_id == demand.id, Application.archived_at.is_(None)), q
    ).order_by(Application.stage_entered_at.desc(), Application.id)
    rows = list((await db.execute(stmt.limit(2000))).scalars())
    outs = await build_outs(db, rows)
    grouped: dict[uuid.UUID, list[ApplicationOut]] = {}
    for item in outs:
        grouped.setdefault(item.stage.id, []).append(item)
    columns = [
        BoardColumn(stage=stages.stage_out(s), count=len(grouped.get(s.id, [])), items=grouped.get(s.id, []))
        for s in all_rows
        if s.archived_at is None or grouped.get(s.id)
    ]
    return BoardOut(columns=columns, total=len(outs))


async def get_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> ApplicationDetailOut:
    from app.services.pipeline import screening as screening_service

    app = await application_for(ctx, db, application_id, READ)
    base = (await build_outs(db, [app]))[0]
    smap = await stages.stage_map(db)
    history_rows = (
        await db.execute(
            select(ApplicationStageHistory)
            .where(ApplicationStageHistory.application_id == app.id)
            .order_by(ApplicationStageHistory.at, ApplicationStageHistory.id)
        )
    ).scalars()
    history = []
    for h in history_rows:
        item = StageHistoryOut.model_validate(h)
        item.from_key = smap[h.from_stage_id].key if h.from_stage_id in smap else None
        item.to_key = smap[h.to_stage_id].key if h.to_stage_id in smap else None
        history.append(item)
    subs = (
        await db.execute(
            select(Submission).where(Submission.application_id == app.id).order_by(Submission.created_at)
        )
    ).scalars()
    return ApplicationDetailOut(
        **base.model_dump(),
        history=history,
        screenings=await screening_service.screenings_for(ctx, db, app),
        submissions=[
            SubmissionBrief(
                id=s.id,
                human_id=s.human_id,
                status=s.status,
                feedback_status=s.feedback_status,
                sent_at=s.sent_at,
            )
            for s in subs
        ],
    )


# ================================================================ endpoint wrappers
async def _permission_for(db: AsyncSession, to_stage_id: uuid.UUID) -> str:
    target = await db.get(PipelineStage, to_stage_id)
    if target is None:
        raise NotFound("Stage not found")
    return REJECT if target.key == "rejected" else MOVE


async def transition(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, data: TransitionIn
) -> ApplicationDetailOut:
    principal_of(ctx)
    await stages.ensure_default_stages(db)
    permission = await _permission_for(db, data.to_stage_id)
    await application_for(ctx, db, application_id, permission)
    await transition_application(
        ctx,
        db,
        application_id,
        data.to_stage_id,
        actor_kind_of(ctx),
        data.reason_code,
        data.note,
        data.expected_stage_id,
        send_rejection_email=data.send_rejection_email,
    )
    return await get_application(ctx, db, application_id)


async def bulk_transition(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: BulkTransitionIn
) -> BulkTransitionOut:
    demand = await demand_for(ctx, db, demand_id, READ)
    await stages.ensure_default_stages(db)
    permission = await _permission_for(db, data.to_stage_id)
    items: list[BulkItemOut] = []
    for app_id in dict.fromkeys(data.application_ids):
        try:
            async with item_savepoint(db):
                app = await application_for(ctx, db, app_id, permission)
                if app.demand_id != demand.id:
                    raise NotFound("Application not found")
                await transition_application(
                    ctx,
                    db,
                    app_id,
                    data.to_stage_id,
                    actor_kind_of(ctx),
                    data.reason_code,
                    data.note,
                    send_rejection_email=data.send_rejection_email,
                )
            items.append(BulkItemOut(application_id=app_id, ok=True))
        except DomainError as exc:
            items.append(BulkItemOut(application_id=app_id, ok=False, error=_error(exc)))
    ok = sum(1 for i in items if i.ok)
    return BulkTransitionOut(items=items, counts={"ok": ok, "failed": len(items) - ok})


async def candidate_applications(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID
) -> list[CandidateApplicationOut]:
    """Candidate 360 card: the caller's visible applications of one candidate."""
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    principal = principal_of(ctx)
    if not await candidates.exists_visible(ctx, db, candidate_id):
        raise NotFound("Candidate not found")
    rows = list(
        (
            await db.execute(
                visible_applications(principal, READ)
                .where(Application.candidate_id == candidate_id)
                .order_by(Application.created_at.desc())
            )
        ).scalars()
    )
    smap = await stages.stage_map(db)
    demands = await demands_port.titles(db, list({a.demand_id for a in rows}))
    return [
        CandidateApplicationOut(
            id=a.id,
            human_id=a.human_id,
            demand_id=a.demand_id,
            demand_title=(demands.get(a.demand_id) or {}).get("title"),
            client_id=(demands.get(a.demand_id) or {}).get("client_id"),
            stage=StageRef(
                id=smap[a.stage_id].id,
                key=smap[a.stage_id].key,
                label=smap[a.stage_id].label,
                kind=smap[a.stage_id].kind,
            ),
            stage_entered_at=a.stage_entered_at,
            last_client_feedback=a.last_client_feedback,
            created_at=a.created_at,
        )
        for a in rows
    ]


async def demand_progress_counts(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    demand_ids: list[uuid.UUID],
) -> dict[uuid.UUID, DemandProgressCounts]:
    """Epic 4 Demand Progress: ``{applications, submissions (sent), interviewing (in the stage)}``."""
    if not demand_ids:
        return {}
    await stages.ensure_default_stages(db)
    out = {d: DemandProgressCounts(applications=0, submissions=0, interviewing=0) for d in demand_ids}
    for did, n in (
        await db.execute(
            select(Application.demand_id, func.count())
            .where(Application.demand_id.in_(demand_ids), Application.archived_at.is_(None))
            .group_by(Application.demand_id)
        )
    ).all():
        out[did].applications = int(n)
    for did, n in (
        await db.execute(
            select(Submission.demand_id, func.count())
            .where(Submission.demand_id.in_(demand_ids), Submission.status == "sent")
            .group_by(Submission.demand_id)
        )
    ).all():
        out[did].submissions = int(n)
    interviewing = await stages.stage_by_key(db, "interviewing")
    for did, n in (
        await db.execute(
            select(Application.demand_id, func.count())
            .where(and_(Application.demand_id.in_(demand_ids), Application.stage_id == interviewing.id))
            .group_by(Application.demand_id)
        )
    ).all():
        out[did].interviewing = int(n)
    return out


async def workflow_snapshot(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    application_id: uuid.UUID,
) -> dict[str, Any]:
    """Ids and enums only (Epic 10 conditions): stage, source, demand facts, screening, last feedback."""
    app = await db.get(Application, application_id)
    if app is None:
        raise NotFound("Application not found")
    from app.services.demands import pipeline_port as demands_port

    stage = await db.get(PipelineStage, app.stage_id)
    demand = await demands_port.get_row(db, app.demand_id)
    return {
        "stage": stage.key if stage else None,
        "source": app.source,
        "demand.priority": demand.priority,
        "demand.client_id": str(demand.client_id),
        "screening_passed": app.screening_passed,
        "last_client_feedback": app.last_client_feedback,
    }
