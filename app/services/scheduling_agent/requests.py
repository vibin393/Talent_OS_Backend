# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Scheduling requests (Epic 59 stories 59.2-59.4; spec A3) and the agent run chain (coordinator C-12).

Lifecycle: ``open`` -> ``slots_offered`` -> ``picked`` -> ``booked``; ``counter_proposed`` (the Candidate asked for other
times) goes back to ``slots_offered`` or ``booked``; ``handed_off`` (no common slot, max rounds, model failure),
``cancelled`` (recruiter, stage exit, interview cancelled) and ``expired`` (link expiry) are terminal. One open request
per (Application, interview stage) (``uq_sched_req_open``).

A negotiation is a chain of short runs: the offer run ends with ``await_event("slot_pick", offer_id)``; a pick resumes it
(the runtime handles ``SlotPicked``) or, when the run already stopped (900 s wall clock), the domain starts a continuation
run (``trigger_key="slot_picked"`` / ``"counter_proposed"``, ``parent_run_id``, dedup ``sched:req:<id>``). State lives
here, never in model history; ``agent_runs`` is joined by ``current_run_id`` (no FK, C-1).
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.sequences import next_human_id
from app.models.scheduling_agent import OPEN_REQUEST_STATUSES, SchedulingRequest, SchedulingSlotOffer
from app.schemas.scheduling_agent import RequestStartIn
from app.services import agent_runtime_service as runtime
from app.services.scheduling_agent import availability, history
from app.services.scheduling_agent.common import (
    AGENT_KEY,
    LINK_PURPOSE,
    LINK_SUBJECT,
    SCHEDULE,
    actor_of,
    agent_on,
    as_user,
    conflict,
    invalid,
    now,
    principal_of,
    request_row,
    require,
    settings_row,
    system_ctx,
    tenant_of,
    user_id_of,
)

INTERVIEWABLE = ("submitted", "interviewing")
ACTIVE_RUN = ("queued", "running", "waiting")


# ================================================================ loaders
async def load_application(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> Any:
    """The Application, readable by the caller (404 otherwise); system contexts read it directly."""
    from app.models.pipeline import Application

    p = principal_of(ctx)
    if p is not None and p.type in ("user", "agent"):
        from app.services import pipeline_service

        return await pipeline_service.applications.application_for(as_user(ctx), db, application_id)
    row = await db.get(Application, application_id)
    if row is None:
        raise NotFound("Application not found")
    return row


async def plan_stage(db: AsyncSession, demand_id: uuid.UUID, stage_id: uuid.UUID) -> Any:
    from app.models.interviews import InterviewStage

    row = await db.get(InterviewStage, stage_id)
    if row is None or row.demand_id != demand_id or row.archived_at is not None:
        raise conflict("stage_not_in_plan", "The interview stage does not belong to this demand's plan")
    return row


async def pipeline_stage_key(db: AsyncSession, stage_id: uuid.UUID) -> str | None:
    from app.services import pipeline_service

    stage = await pipeline_service.stages.get_stage(db, stage_id)
    return getattr(stage, "key", None)


async def _active_users(db: AsyncSession, user_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    from app.services import entity_refs

    ids = list(dict.fromkeys(user_ids))
    active = await entity_refs.active_user_ids(db, ids)
    missing = [str(u) for u in ids if u not in active]
    if missing:
        raise invalid("invalid_participant", "Unknown or inactive interviewer", user_ids=missing)
    return ids


async def open_request_for(
    db: AsyncSession, application_id: uuid.UUID, stage_id: uuid.UUID
) -> SchedulingRequest | None:
    return (
        await db.execute(
            select(SchedulingRequest).where(
                SchedulingRequest.application_id == application_id,
                SchedulingRequest.stage_id == stage_id,
                SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def _create(
    ctx: RequestContext,
    db: AsyncSession,
    app: Any,
    stage: Any,
    *,
    kind: str,
    participants: list[uuid.UUID],
    external: list[dict[str, Any]],
    duration: int,
    buffer: int,
    mode: str | None,
    candidate_tz: str,
    interview_id: uuid.UUID | None = None,
    reschedule_reason: str | None = None,
    requested_by: tuple[str, uuid.UUID | None] | None = None,
) -> SchedulingRequest:
    kind_by, by_id = requested_by or actor_of(ctx)
    req = SchedulingRequest(
        id=uuid.uuid4(), tenant_id=tenant_of(db), human_id=await next_human_id(db, "scheduling_request", "SCH"),
        application_id=app.id, candidate_id=app.candidate_id, demand_id=app.demand_id, stage_id=stage.id,
        interview_id=interview_id, kind=kind, status="open", round=0, duration_minutes=duration, buffer_minutes=buffer,
        participant_user_ids=participants, external_participants=external, mode=mode, candidate_timezone=candidate_tz,
        owner_user_id=app.owner_id, requested_by_kind=kind_by[:20], requested_by_id=by_id,
        reschedule_reason=reschedule_reason,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            db.add(req)
            await db.flush()
    except IntegrityError as exc:
        raise conflict(
            "scheduling_request_open", "A scheduling request is already open for this stage"
        ) from exc
    history.record(
        db, req, "requested", actor_kind=kind_by, actor_id=by_id,
        detail={"kind": kind, "stage_id": stage.id, "participants": len(participants), "interview_id": interview_id},
    )  # fmt: skip
    audit.record("scheduling_request.created", "scheduling_request", req.id, session=db, kind=kind)
    return req


async def _validate_timezone(tz: str) -> str:  # NOSONAR
    from app.services.interviews import timeutil

    timeutil.zone(tz)  # 422 invalid_timezone
    return tz


# ================================================================ start ("Let the agent schedule")
async def options(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, stage_id: uuid.UUID
) -> dict[str, Any]:
    app = await load_application(ctx, db, application_id)
    stage = await plan_stage(db, app.demand_id, stage_id)
    tz, source = await availability.candidate_timezone(db, app.candidate_id)
    existing = await open_request_for(db, app.id, stage.id)
    return {
        "agent_on": await agent_on(db), "stage_id": stage.id, "stage_name": stage.name,
        "duration_minutes": stage.default_duration_minutes, "candidate_timezone": tz, "timezone_source": source,
        "open_request_id": existing.id if existing is not None else None,
    }  # fmt: skip


async def start(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    data: RequestStartIn,
    *,
    trigger_kind: str = "manual",
) -> SchedulingRequest:
    """Open a request and start its first run. 409: ``scheduling_agent_off``, ``application_not_interviewable``,
    ``stage_not_in_plan``, ``scheduling_request_open``, ``scheduling_agent_unavailable``; 422 ``participants_required``."""
    require(ctx, SCHEDULE)
    if not await agent_on(db):
        raise conflict("scheduling_agent_off", "The Scheduling Agent is off")
    app = await load_application(ctx, db, application_id)
    if await pipeline_stage_key(db, app.stage_id) not in INTERVIEWABLE:
        raise conflict(
            "application_not_interviewable",
            "Interviews can be scheduled only for applications in Submitted or Interviewing",
        )
    stage = await plan_stage(db, app.demand_id, data.stage_id)
    if not data.participant_user_ids:
        raise invalid("participants_required", "Choose at least one interviewer")
    participants = await _active_users(db, data.participant_user_ids)
    settings = await settings_row(db)
    tz = data.candidate_timezone or (await availability.candidate_timezone(db, app.candidate_id))[0]
    req = await _create(
        ctx, db, app, stage, kind="new", participants=participants, external=[],
        duration=data.duration_minutes or stage.default_duration_minutes, buffer=settings.default_buffer_minutes,
        mode=data.mode or "video", candidate_tz=await _validate_timezone(tz),
    )  # fmt: skip
    ref = await start_run(ctx, db, req, trigger_kind=trigger_kind, trigger_key="requested", mode="offer")
    if ref.run_id is None:
        raise conflict(
            "scheduling_agent_unavailable",
            "The Scheduling Agent cannot run now",
            reason=ref.reason or "not_started",
        )
    return req


async def ensure_for_proposal(ctx: RequestContext, db: AsyncSession, app: Any, req: Any) -> SchedulingRequest:
    """The open request a ``propose`` call belongs to (created when the Copilot proposes without one)."""
    from app.models.interviews import Interview

    if req.interview_id is not None:
        interview = await db.get(Interview, req.interview_id)
        if interview is None or interview.application_id != app.id:
            raise NotFound("Interview not found")
        row = (
            await db.execute(
                select(SchedulingRequest).where(
                    SchedulingRequest.interview_id == interview.id,
                    SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES),
                )
            )
        ).scalar_one_or_none()
        stage_id = interview.stage_id
    else:
        if req.interview_stage_id is None:
            raise invalid("stage_required", "interview_stage_id or interview_id is required")
        stage_id = req.interview_stage_id
        row = await open_request_for(db, app.id, stage_id)
    if row is not None:
        return row
    stage = await plan_stage(db, app.demand_id, stage_id)
    participants = await _active_users(db, list(req.participant_user_ids))
    if not participants:
        raise invalid("participants_required", "Choose at least one interviewer")
    return await _create(
        ctx, db, app, stage, kind="reschedule" if req.interview_id else "new", participants=participants, external=[],
        duration=req.duration_minutes, buffer=req.buffer_minutes, mode="video",
        candidate_tz=await _validate_timezone(req.candidate_timezone), interview_id=req.interview_id,
    )  # fmt: skip


# ================================================================ reads
async def readable(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> SchedulingRequest:
    req = await request_row(db, request_id)
    try:
        await load_application(ctx, db, req.application_id)
    except NotFound as exc:
        raise NotFound("Scheduling request not found") from exc
    return req


async def list_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[SchedulingRequest]:
    app = await load_application(ctx, db, application_id)
    return list(
        (
            await db.execute(
                select(SchedulingRequest)
                .where(SchedulingRequest.application_id == app.id)
                .order_by(SchedulingRequest.created_at.desc())
            )
        ).scalars()
    )


async def offers_of(db: AsyncSession, request_id: uuid.UUID) -> list[SchedulingSlotOffer]:
    return list(
        (
            await db.execute(
                select(SchedulingSlotOffer)
                .where(SchedulingSlotOffer.request_id == request_id)
                .order_by(SchedulingSlotOffer.round, SchedulingSlotOffer.created_at)
            )
        ).scalars()
    )


async def latest_offer(db: AsyncSession, request_id: uuid.UUID, *statuses: str) -> SchedulingSlotOffer | None:
    stmt = select(SchedulingSlotOffer).where(SchedulingSlotOffer.request_id == request_id)
    if statuses:
        stmt = stmt.where(SchedulingSlotOffer.status.in_(statuses))
    return (
        await db.execute(
            stmt.order_by(SchedulingSlotOffer.round.desc(), SchedulingSlotOffer.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


# ================================================================ closing
async def revoke_links(db: AsyncSession, req: SchedulingRequest, *, offer_status: str = "expired") -> None:
    for offer in await offers_of(db, req.id):
        await secure_links.revoke_for_subject(db, LINK_SUBJECT, offer.id, LINK_PURPOSE)
        if offer.status in ("draft", "open"):
            offer.status = offer_status
    await db.flush()


async def close_offers(db: AsyncSession, req: SchedulingRequest) -> None:
    """Booked: no offer can be picked any more (open / draft -> expired). The links stay readable so the Candidate
    sees "You're booked" and a repeated pick returns the booking (``deduplicated``)."""
    for offer in await offers_of(db, req.id):
        if offer.status in ("draft", "open"):
            offer.status = "expired"
    await db.flush()


async def _cancel_run(db: AsyncSession, req: SchedulingRequest, reason: str) -> None:
    from app.models.agent_runtime import AgentRun

    if req.current_run_id is None:
        return
    run = await db.get(AgentRun, req.current_run_id)
    if run is None or run.status not in ACTIVE_RUN:
        return
    try:
        async with db.begin_nested():
            await runtime.cancel_run(
                system_ctx(req.tenant_id), db, run.id, reason=reason, reason_code="subject_closed"
            )
    except Exception:  # pragma: no cover - a run finishing concurrently
        logger.info("Scheduling {}: run {} not cancelled", req.id, run.id)


async def close(
    ctx: RequestContext,
    db: AsyncSession,
    req: SchedulingRequest,
    status: str,
    *,
    reason: str,
    cancel_run: bool = True,
) -> SchedulingRequest:
    """``cancelled`` / ``expired``: links revoked, open offers expired, the run cancelled (``subject_closed``)."""
    if req.status not in OPEN_REQUEST_STATUSES:
        return req
    kind, actor_id = actor_of(ctx)
    await revoke_links(db, req)
    history.set_status(db, req, status)
    history.record(
        db,
        req,
        "cancelled" if status == "cancelled" else "expired",
        actor_kind=kind,
        actor_id=actor_id,
        detail={"reason": reason[:60]},
    )
    if cancel_run:
        await _cancel_run(db, req, f"scheduling request {status}")
    await db.flush()
    return req


async def cancel(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, reason: str | None = None
) -> SchedulingRequest:
    require(ctx, SCHEDULE)
    req = await readable(ctx, db, request_id)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise conflict(
            "scheduling_request_closed", "The scheduling request is no longer open", status=req.status
        )
    await close(ctx, db, req, "cancelled", reason=reason or "cancelled")
    audit.record("scheduling_request.cancelled", "scheduling_request", req.id, session=db)
    return req


async def close_for_application(db: AsyncSession, application_id: uuid.UUID, reason: str) -> int:
    rows = list(
        (
            await db.execute(
                select(SchedulingRequest)
                .where(
                    SchedulingRequest.application_id == application_id,
                    SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for req in rows:
        await close(system_ctx(req.tenant_id), db, req, "cancelled", reason=reason)
    return len(rows)


async def close_for_interview(db: AsyncSession, interview_id: uuid.UUID, reason: str) -> int:
    rows = list(
        (
            await db.execute(
                select(SchedulingRequest)
                .where(
                    SchedulingRequest.interview_id == interview_id,
                    SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for req in rows:
        await close(system_ctx(req.tenant_id), db, req, "cancelled", reason=reason)
    return len(rows)


async def expire_due(db: AsyncSession) -> int:
    """Open offers past ``expires_at``: the offer and its request expire (links revoked, run cancelled)."""
    at = now()
    offers = list(
        (
            await db.execute(
                select(SchedulingSlotOffer)
                .where(SchedulingSlotOffer.status == "open", SchedulingSlotOffer.expires_at < at)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    count = 0
    for offer in offers:
        req = await request_row(db, offer.request_id, for_update=True)
        offer.status = "expired"
        if req.status == "slots_offered":
            await close(system_ctx(req.tenant_id), db, req, "expired", reason="link_expired")
            count += 1
    await db.flush()
    return count


async def _task(db: AsyncSession, req: SchedulingRequest, title: str, notes: str) -> uuid.UUID | None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    data = TaskCreate.model_construct(
        title=title[:200], notes=f"{notes}\n\nScheduling request {req.human_id}."[:4000], owner_id=req.owner_user_id,
        entity_type="application", entity_id=req.application_id, due_at=None, due_all_day=False, priority="high",
        reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            out = await tasks_service.create_system_task(
                RequestContext(tenant_id=req.tenant_id), db, data, source="agent"
            )
        return out.id
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.warning("Scheduling {}: could not create the task {!r}", req.id, title)
        return None


async def hand_off(
    ctx: RequestContext, db: AsyncSession, req: SchedulingRequest, reason: str, note: str | None = None
) -> uuid.UUID | None:
    """Give the request to the recruiter: status ``handed_off``, a Task with the latest Candidate windows."""
    if req.status not in OPEN_REQUEST_STATUSES:
        return None
    from app.models.candidates import Candidate
    from app.models.interviews import InterviewStage

    cand = await db.get(Candidate, req.candidate_id)
    stage = await db.get(InterviewStage, req.stage_id)
    name = getattr(cand, "full_name", None) or "the candidate"
    latest = await latest_offer(db, req.id, "declined")
    windows = (latest.counter_windows or []) if latest is not None else []
    lines = [note or "The Scheduling Agent could not agree a time and handed the request to you."]
    if windows:
        lines.append(
            "Times the candidate suggested (UTC): "
            + "; ".join(f"{w.get('start')} - {w.get('end')}" for w in windows)
        )
    lines.append("Schedule it manually from the application's Interviews tab.")
    req.handoff_reason = reason
    await revoke_links(db, req)
    history.set_status(db, req, "handed_off")
    kind, actor_id = actor_of(ctx)
    task_id = await _task(
        db, req, f"Scheduling needs you: {name}, {stage.name if stage else 'interview'}", "\n".join(lines)
    )
    history.record(
        db,
        req,
        "handed_off",
        actor_kind=kind,
        actor_id=actor_id,
        detail={"reason": reason, "task_id": task_id},
    )
    await db.flush()
    return task_id


# ================================================================ reschedule requests
async def request_reschedule(
    ctx: RequestContext,
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    by: str,
    reason: str,
    candidate_id: uuid.UUID | None = None,
) -> SchedulingRequest:
    """``kind=reschedule`` request for a booked Interview: by an interviewer (participant), the Candidate (portal /
    reminder link) or a recruiter (``interview:schedule``). 409 ``interview_not_scheduled``, ``scheduling_agent_off``."""
    from app.models.interviews import Interview, InterviewParticipant

    row = await db.get(Interview, interview_id)
    if row is None:
        raise NotFound("Interview not found")
    if by == "candidate":
        if candidate_id is None or row.candidate_id != candidate_id:
            raise NotFound("Interview not found")
    else:
        uid = user_id_of(ctx)
        parts = (
            (
                await db.execute(
                    select(InterviewParticipant).where(
                        InterviewParticipant.interview_id == row.id, InterviewParticipant.removed_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        is_participant = uid is not None and (
            uid == row.organizer_user_id or any(p.user_id == uid for p in parts)
        )
        if not is_participant:
            require(ctx, SCHEDULE)
            await load_application(ctx, db, row.application_id)
        by = "interviewer" if is_participant and by != "recruiter" else by
    if row.status != "scheduled":
        raise conflict(
            "interview_not_scheduled", "Only scheduled interviews can be rescheduled", status=row.status
        )
    if not await agent_on(db):
        raise conflict("scheduling_agent_off", "The Scheduling Agent is off")
    existing = (
        await db.execute(
            select(SchedulingRequest).where(
                SchedulingRequest.interview_id == row.id, SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES)
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    parts = (
        (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == row.id, InterviewParticipant.removed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    internal = [p.user_id for p in parts if p.kind == "interviewer_internal" and p.user_id is not None]
    external = [{"email": p.email, "name": p.name} for p in parts if p.kind == "interviewer_external"]
    cand = next((p for p in parts if p.kind == "candidate"), None)
    app = await load_application(system_ctx(row.tenant_id), db, row.application_id)
    stage = await plan_stage(db, app.demand_id, row.stage_id)
    duration = int((row.ends_at - row.starts_at).total_seconds() // 60)
    requested_by = ("candidate", candidate_id) if by == "candidate" else actor_of(ctx)
    req = await _create(
        ctx, db, app, stage, kind="reschedule", participants=internal, external=external, duration=duration,
        buffer=(await settings_row(db)).default_buffer_minutes, mode=row.mode,
        candidate_tz=cand.timezone if cand is not None else row.timezone, interview_id=row.id,
        reschedule_reason=reason[:300], requested_by=(requested_by[0] if by != "interviewer" else "interviewer", requested_by[1]),
    )  # fmt: skip
    await start_run(
        system_ctx(req.tenant_id) if by == "candidate" else ctx,
        db,
        req,
        trigger_kind="manual",
        trigger_key="reschedule_requested",
        mode="reschedule",
    )
    return req


async def request_reschedule_system(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument] - facade signature; the request runs as the system actor
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    by: str,
    participant_id: uuid.UUID | None,
    reason: str,
    proposed_start: datetime | None = None,
    proposed_end: datetime | None = None,
) -> SchedulingRequest | None:
    """Epic 72 (D-18, additive): ``request_reschedule`` without a user principal -- a calendar change (decline / move /
    deletion) asks the Scheduling Agent for a new time. System actor (audited as ``system``, source ``calendar_sync``).
    Returns None when the agent is off or the Interview is not scheduled (the caller creates a Task instead); an open
    request for the Interview is returned as is (dedup)."""
    from app.models.interviews import Interview, InterviewParticipant

    row = await db.get(Interview, interview_id)
    if row is None or row.status != "scheduled" or not await agent_on(db):
        return None
    existing = (
        await db.execute(
            select(SchedulingRequest).where(
                SchedulingRequest.interview_id == row.id, SchedulingRequest.status.in_(OPEN_REQUEST_STATUSES)
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    parts = (
        (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == row.id, InterviewParticipant.removed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    sctx = system_ctx(row.tenant_id)
    internal = [p.user_id for p in parts if p.kind == "interviewer_internal" and p.user_id is not None]
    external = [{"email": p.email, "name": p.name} for p in parts if p.kind == "interviewer_external"]
    cand = next((p for p in parts if p.kind == "candidate"), None)
    app = await load_application(sctx, db, row.application_id)
    stage = await plan_stage(db, app.demand_id, row.stage_id)
    duration = int((row.ends_at - row.starts_at).total_seconds() // 60)
    note = reason
    if proposed_start is not None:
        note = f"{reason}; proposed {_iso(proposed_start)}" + (f" - {_iso(proposed_end)}" if proposed_end else "")
    req = await _create(
        sctx, db, app, stage, kind="reschedule", participants=internal, external=external, duration=duration,
        buffer=(await settings_row(db)).default_buffer_minutes, mode=row.mode,
        candidate_tz=cand.timezone if cand is not None else row.timezone, interview_id=row.id,
        reschedule_reason=note[:300], requested_by=("candidate" if by == "candidate" else "interviewer", None),
    )  # fmt: skip
    audit.record(
        "scheduling_request.calendar_change", "scheduling_request", req.id, session=db,
        source="calendar_sync", by=by, participant_id=str(participant_id) if participant_id else None,
    )  # fmt: skip
    await start_run(sctx, db, req, trigger_kind="manual", trigger_key="reschedule_requested", mode="reschedule")
    return req


# ================================================================ runs
async def kickoff(
    db: AsyncSession,
    req: SchedulingRequest,
    mode: str,
    *,
    offer_id: uuid.UUID | None = None,
    pick_id: uuid.UUID | None = None,
    windows: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> Kickoff:
    settings = await settings_row(db)
    inputs = {
        "mode": mode, "request_id": str(req.id), "application_id": str(req.application_id),
        "candidate_id": str(req.candidate_id), "stage_id": str(req.stage_id), "request_kind": req.kind,
        "request_status": req.status, "round": str(req.round), "max_rounds": str(settings.max_rounds),
        "channel": "email",
    }  # fmt: skip
    if req.interview_id:
        inputs["interview_id"] = str(req.interview_id)
    if offer_id:
        inputs["offer_id"] = str(offer_id)
    if pick_id:
        inputs["pick_id"] = str(pick_id)
    if windows:
        inputs["windows"] = json.dumps(windows)
    untrusted = {"candidate_note": note} if note else {}
    return Kickoff(inputs=inputs, untrusted=untrusted, candidate_ids=(req.candidate_id,))


async def start_run(
    ctx: RequestContext,
    db: AsyncSession,
    req: SchedulingRequest,
    *,
    trigger_kind: str,
    trigger_key: str,
    mode: str,
    offer_id: uuid.UUID | None = None,
    pick_id: uuid.UUID | None = None,
    windows: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> Any:
    kick = await kickoff(db, req, mode, offer_id=offer_id, pick_id=pick_id, windows=windows, note=note)
    ref = await runtime.start_run(
        ctx, db, agent_key=AGENT_KEY, trigger_kind=trigger_kind, trigger_key=trigger_key,
        subject=("application", req.application_id), kickoff=kick, owner_user_id=req.owner_user_id,
        dedup_key=f"sched:req:{req.id}", parent_run_id=req.current_run_id,
    )  # fmt: skip
    if ref.run_id is not None and ref.reason not in ("attached", "deduplicated"):
        req.current_run_id = ref.run_id
        await db.flush()
    return ref


async def wake(db: AsyncSession, request_id: uuid.UUID, trigger_key: str, offer_id: uuid.UUID | None) -> str:
    """Hand a pick / counter-proposal to the agent (after commit). Returns ``resumed`` | ``runtime`` | ``continued`` |
    ``retry`` | ``skipped`` | ``not_started``."""
    from app.models.agent_runtime import AgentRun

    req = await request_row(db, request_id, for_update=True)
    wanted = {"slot_picked": ("picked",), "counter_proposed": ("counter_proposed",)}.get(
        trigger_key, OPEN_REQUEST_STATUSES
    )
    if req.status not in wanted:
        return "skipped"
    ctx = system_ctx(req.tenant_id)
    offer = await db.get(SchedulingSlotOffer, offer_id) if offer_id else None
    run = await db.get(AgentRun, req.current_run_id) if req.current_run_id else None
    if run is not None and run.status == "waiting":
        live = run.deadline_at is None or run.deadline_at > now()
        if live and offer_id is not None and run.wait_condition == "slot_pick" and run.wait_ref == offer_id:
            if trigger_key == "slot_picked":
                return "runtime"  # the runtime resumes runs waiting on ("slot_pick", offer) for SlotPicked
            refs = await runtime.resume(
                ctx, db, condition="slot_pick", ref=offer_id,
                payload={"event": "counter_proposed", "request_id": str(req.id), "windows": offer.counter_windows if offer else []},
                untrusted_text=offer.candidate_note if offer is not None and offer.candidate_note else None,
            )  # fmt: skip
            if refs:
                return "resumed"
        # an expired wait, or a wait on something else: replace it with a continuation run
        await runtime.cancel_run(
            ctx, db, run.id, reason="replaced by a continuation run", reason_code="subject_closed"
        )
    elif run is not None and run.status in ("queued", "running"):
        return "retry"
    mode = "picked" if trigger_key == "slot_picked" else "counter"
    ref = await start_run(
        ctx, db, req, trigger_kind="inbound", trigger_key=trigger_key, mode=mode, offer_id=offer_id,
        pick_id=offer.pick_id if offer is not None else None,
        windows=offer.counter_windows if offer is not None and trigger_key == "counter_proposed" else None,
        note=offer.candidate_note if offer is not None and trigger_key == "counter_proposed" else None,
    )  # fmt: skip
    return "continued" if ref.run_id is not None else "not_started"


async def on_run_failed(db: AsyncSession, run_id: uuid.UUID, reason_code: str) -> str | None:
    """A failed run hands its open request to the recruiter (``model_failure``)."""
    req = (
        await db.execute(
            select(SchedulingRequest).where(SchedulingRequest.current_run_id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if req is None or req.status not in OPEN_REQUEST_STATUSES:
        return None
    await hand_off(
        system_ctx(req.tenant_id), db, req, "model_failure", f"The Scheduling Agent stopped ({reason_code})."
    )
    return "handed_off"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "cancel",
    "close",
    "close_for_application",
    "close_for_interview",
    "close_offers",
    "ensure_for_proposal",
    "expire_due",
    "hand_off",
    "kickoff",
    "latest_offer",
    "list_for_application",
    "load_application",
    "offers_of",
    "on_run_failed",
    "open_request_for",
    "options",
    "plan_stage",
    "readable",
    "request_reschedule",
    "revoke_links",
    "start",
    "start_run",
    "wake",
]
