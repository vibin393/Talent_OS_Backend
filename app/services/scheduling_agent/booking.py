# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Booking through the P1 scheduler (Epic 59 stories 59.3 / 59.4; spec A3).

``book`` re-checks free/busy for the picked slot (or a Candidate-proposed window) and books through
``interviews_service.schedule_interview`` (invites, ICS / provider event, meeting link). ``reschedule`` moves the booked
Interview through ``interviews_service.reschedule_interview`` (P1 bumps ``sequence``, keeps ``ical_uid``). Both are
idempotent: a request already booked returns its Interview (``deduplicated``); a duplicate pick never double-books.

A slot that is no longer free, or outside someone's working hours / on a holiday, is a ``slot_conflict`` result (the
agent re-offers) unless ``accept_conflicts`` -- which only an approved Proposal reaches (the tool's ``risk_fn`` makes it
``high``). A P1 refusal (e.g. ``application_not_interviewable``) hands the request to the recruiter with a Task.

Cancelling an Interview and changing its interviewers are always Proposals (``interviews.cancel`` /
``interviews.change_participants`` are ``high``); here they execute as the approving person.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.models.scheduling_agent import OPEN_REQUEST_STATUSES, SchedulingRequest, SchedulingSlotOffer
from app.services import interviews_service
from app.services.scheduling_agent import availability, history, requests, service
from app.services.scheduling_agent.common import (
    actor_of,
    as_user,
    conflict,
    invalid,
    principal_of,
    request_row,
    system_ctx,
)


async def _p1_ctx(ctx: RequestContext, db: AsyncSession, req: SchedulingRequest) -> RequestContext:
    """P1 interviews need a principal: agent -> the Accountable Owner with the Effective Rights (the organiser is the
    owner; actor stays ``agent``); approved Proposals run as the approver; system contexts as the owner."""
    p = principal_of(ctx)
    if p is not None:
        return as_user(ctx)
    from dataclasses import replace

    from app.services import entity_refs

    owner = await entity_refs.principal_for_user(db, req.tenant_id, req.owner_user_id)
    if owner is None:
        raise conflict("owner_unavailable", "The request owner is not active")
    return replace(ctx, principal=owner)


def _slot_of(offer: SchedulingSlotOffer) -> tuple[datetime, datetime]:
    if offer.picked_index is None or not 0 <= offer.picked_index < len(offer.slots or []):
        raise conflict("no_pick", "The candidate has not picked a time")
    s = offer.slots[offer.picked_index]
    return datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])


async def _resolve_slot(
    db: AsyncSession,
    req: SchedulingRequest,
    pick_id: uuid.UUID | None,
    window: tuple[datetime, datetime] | None,
) -> tuple[datetime, datetime, SchedulingSlotOffer | None, str]:
    from sqlalchemy import select

    if window is not None:
        start = window[0]
        end = start + timedelta(minutes=int(req.duration_minutes))
        latest = await requests.latest_offer(db, req.id, "declined")
        proposed = [
            (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
            for w in ((latest.counter_windows or []) if latest is not None else [])
        ]
        matched = any(a <= start and end <= b for a, b in proposed)
        return start, end, latest, "candidate_window" if matched else "recruiter_window"
    if pick_id is not None:
        offer = (
            await db.execute(
                select(SchedulingSlotOffer).where(
                    SchedulingSlotOffer.pick_id == pick_id, SchedulingSlotOffer.request_id == req.id
                )
            )
        ).scalar_one_or_none()
    else:
        offer = await requests.latest_offer(db, req.id, "picked")
    if offer is None or offer.status != "picked":
        raise conflict("no_pick", "The candidate has not picked a time")
    start, end = _slot_of(offer)
    return start, end, offer, "candidate_pick"


async def _participants(_db: AsyncSession, req: SchedulingRequest) -> list[Any]:
    from app.schemas.interviews import ParticipantIn

    out = [ParticipantIn(kind="interviewer_internal", user_id=u) for u in req.participant_user_ids]
    out += [
        ParticipantIn(kind="interviewer_external", email=e["email"], name=e.get("name"))
        for e in req.external_participants or []
        if e.get("email")
    ]
    return out


def _result(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, **extra}


async def book(
    ctx: RequestContext,
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    pick_id: uuid.UUID | None = None,
    window: tuple[datetime, datetime] | None = None,
    accept_conflicts: bool = False,
) -> dict[str, Any]:
    """``{"status": "booked" | "deduplicated" | "slot_conflict" | "handed_off", "interview_id", "starts_at", ...}``."""
    req = await request_row(db, request_id, for_update=True)
    if req.kind == "reschedule":
        return await reschedule(
            ctx, db, request_id, pick_id=pick_id, window=window, accept_conflicts=accept_conflicts
        )
    if req.status == "booked" and req.booked_interview_id is not None:
        out = await interviews_service.get_interview(await _p1_ctx(ctx, db, req), db, req.booked_interview_id)
        return _result("deduplicated", interview_id=out.id, starts_at=out.starts_at, sequence=out.sequence)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise conflict(
            "scheduling_request_closed", "The scheduling request is no longer open", status=req.status
        )
    start, end, offer, source = await _resolve_slot(db, req, pick_id, window)
    check = await service.recheck(ctx, db, req, start, end)
    kind, actor_id = actor_of(ctx)
    if not check["ok"] and not accept_conflicts:
        history.record(
            db, req, "conflict", actor_kind=kind, actor_id=actor_id,
            detail={"start": start, "busy": check["busy"], "outside_hours": check["hard_fail"], "offer_id": offer.id if offer else None},
        )  # fmt: skip
        if offer is not None and offer.status == "picked":
            offer.status, offer.picked_index = "superseded", None
        await db.flush()
        return _result(
            "slot_conflict", busy=[str(u) for u in check["busy"]], outside_working_hours=check["hard_fail"]
        )
    if source == "recruiter_window" and not accept_conflicts:
        raise invalid("window_not_proposed", "The window is not one the candidate proposed")
    tz = await availability.organiser_timezone(db, req.owner_user_id)
    from app.schemas.interviews import InterviewCreate
    from app.services.interviews import timeutil

    local = start.astimezone(timeutil.zone(tz)).replace(tzinfo=None).isoformat(timespec="minutes")
    data = InterviewCreate(
        stage_id=req.stage_id, start_local=local, timezone=tz, duration_minutes=int(req.duration_minutes),
        mode=req.mode or "video", create_online_meeting=(req.mode or "video") == "video", send_invites=True,
        candidate_timezone=req.candidate_timezone, participants=await _participants(db, req),
    )  # fmt: skip
    p1 = await _p1_ctx(ctx, db, req)
    try:
        async with db.begin_nested():
            out = await interviews_service.schedule_interview(p1, db, req.application_id, data)
    except DomainError as exc:
        task_id = await requests.hand_off(
            ctx, db, req, "other", f"Booking failed in the interview scheduler ({exc.code})."
        )
        return _result("handed_off", error=exc.code, task_id=str(task_id) if task_id else None)
    req.booked_interview_id = out.id
    history.set_status(db, req, "booked")
    await requests.close_offers(db, req)
    history.record(
        db, req, "booked", actor_kind=kind, actor_id=actor_id,
        detail={"interview_id": out.id, "human_id": out.human_id, "start": out.starts_at, "source": source, "accept_conflicts": accept_conflicts},
    )  # fmt: skip
    audit.record(
        "scheduling_request.booked", "scheduling_request", req.id, session=db, interview_id=str(out.id)
    )
    await db.flush()
    return _result(
        "booked", interview_id=out.id, starts_at=out.starts_at, human_id=out.human_id, sequence=out.sequence
    )


async def reschedule(
    ctx: RequestContext,
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    pick_id: uuid.UUID | None = None,
    window: tuple[datetime, datetime] | None = None,
    accept_conflicts: bool = False,
) -> dict[str, Any]:
    req = await request_row(db, request_id, for_update=True)
    if req.kind != "reschedule" or req.interview_id is None:
        raise conflict("not_a_reschedule", "Only reschedule requests move an interview")
    p1 = await _p1_ctx(ctx, db, req)
    if req.status == "booked":
        out = await interviews_service.get_interview(p1, db, req.interview_id)
        return _result("deduplicated", interview_id=out.id, starts_at=out.starts_at, sequence=out.sequence)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise conflict(
            "scheduling_request_closed", "The scheduling request is no longer open", status=req.status
        )
    start, end, offer, source = await _resolve_slot(db, req, pick_id, window)
    check = await service.recheck(ctx, db, req, start, end)
    kind, actor_id = actor_of(ctx)
    if not check["ok"] and not accept_conflicts:
        history.record(
            db,
            req,
            "conflict",
            actor_kind=kind,
            actor_id=actor_id,
            detail={"start": start, "busy": check["busy"], "outside_hours": check["hard_fail"]},
        )
        if offer is not None and offer.status == "picked":
            offer.status, offer.picked_index = "superseded", None
        await db.flush()
        return _result(
            "slot_conflict", busy=[str(u) for u in check["busy"]], outside_working_hours=check["hard_fail"]
        )
    if source == "recruiter_window" and not accept_conflicts:
        raise invalid("window_not_proposed", "The window is not one the candidate proposed")
    from app.schemas.interviews import InterviewReschedule
    from app.services.interviews import timeutil

    current = await interviews_service.get_interview(p1, db, req.interview_id)
    tz = current.timezone
    local = start.astimezone(timeutil.zone(tz)).replace(tzinfo=None).isoformat(timespec="minutes")
    data = InterviewReschedule(
        start_local=local, timezone=tz, duration_minutes=int(req.duration_minutes), expected_version=current.version,
        candidate_timezone=req.candidate_timezone,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            out = await interviews_service.reschedule_interview(p1, db, req.interview_id, data)
    except DomainError as exc:
        task_id = await requests.hand_off(
            ctx, db, req, "other", f"Rescheduling failed in the interview scheduler ({exc.code})."
        )
        return _result("handed_off", error=exc.code, task_id=str(task_id) if task_id else None)
    req.booked_interview_id = out.id
    history.set_status(db, req, "booked")
    await requests.close_offers(db, req)
    history.record(
        db,
        req,
        "rescheduled",
        actor_kind=kind,
        actor_id=actor_id,
        detail={"interview_id": out.id, "start": out.starts_at, "sequence": out.sequence, "source": source},
    )
    await db.flush()
    return _result(
        "booked", interview_id=out.id, starts_at=out.starts_at, sequence=out.sequence, human_id=out.human_id
    )


async def cancel_interview(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    """Executes an approved ``interviews.cancel`` Proposal (as the approver)."""
    from app.schemas.interviews import CancelIn

    out = await interviews_service.cancel_interview(as_user(ctx), db, interview_id, CancelIn(reason=reason))
    return {"status": out.status}


async def change_participants(
    ctx: RequestContext,
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    add_user_ids: list[uuid.UUID],
    remove_user_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """Executes an approved ``interviews.change_participants`` Proposal: the P1 reschedule with the new participant list
    at the same time (sequence bumped, same UID)."""
    from app.schemas.interviews import InterviewReschedule, ParticipantIn

    uctx = as_user(ctx)
    current = await interviews_service.get_interview(uctx, db, interview_id)
    keep: list[ParticipantIn] = []
    for p in current.participants:
        if p.kind == "candidate" or (p.user_id is not None and p.user_id in remove_user_ids):
            continue
        if p.kind == "interviewer_internal":
            keep.append(
                ParticipantIn(
                    kind="interviewer_internal", user_id=p.user_id, scorecard_required=p.scorecard_required
                )
            )
        elif p.kind == "interviewer_external":
            keep.append(
                ParticipantIn(
                    kind="interviewer_external",
                    email=p.email,
                    name=p.name,
                    scorecard_required=p.scorecard_required,
                )
            )
        else:
            keep.append(
                ParticipantIn(kind="observer", user_id=p.user_id)
                if p.user_id
                else ParticipantIn(kind="observer", email=p.email, name=p.name)
            )
    present = {p.user_id for p in keep if p.user_id}
    keep += [ParticipantIn(kind="interviewer_internal", user_id=u) for u in add_user_ids if u not in present]
    data = InterviewReschedule(
        start_local=current.start_local, timezone=current.timezone, duration_minutes=current.duration_minutes,
        expected_version=current.version, participants=keep,
    )  # fmt: skip
    out = await interviews_service.reschedule_interview(uctx, db, interview_id, data)
    return {"interview_id": out.id, "sequence": out.sequence}


async def interview_for_tool(
    db: AsyncSession, interview_id: uuid.UUID, application_id: uuid.UUID | None
) -> Any:
    """The Interview, bound to the run's Application when the run has one (else ``not_found``)."""
    from app.models.interviews import Interview

    row = await db.get(Interview, interview_id)
    if row is None or (application_id is not None and row.application_id != application_id):
        raise NotFound("Interview not found")
    return row


def system(tenant_id: uuid.UUID) -> RequestContext:
    return system_ctx(tenant_id)


__all__ = ["book", "cancel_interview", "change_participants", "interview_for_tool", "reschedule"]
