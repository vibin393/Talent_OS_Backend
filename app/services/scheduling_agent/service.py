# ruff: noqa: E501
"""Scheduling agent contract (Epic 53 contract layer, AD-55, spec 0.12) -- filled by Epic 59 (story 59.1).

``propose(ctx, db, req)`` computes realistic interview slots from the participants' free/busy (connected calendars),
working hours, holidays, the stage duration and buffers, and the Candidate's time zone; it stores them as a ``draft``
``scheduling_slot_offers`` row whose id is the ``proposal_ref``. Free/busy only; no tentative holds. The signature is
pinned by ``tests/contracts/signatures.json`` (Epic 54's Copilot wraps it).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.scheduling_agent import SchedulingRequest, SchedulingSlotOffer
from app.schemas.scheduling_agent import Slot, SlotProposalIn, SlotProposalOut
from app.services.scheduling_agent import availability, history, requests, slots
from app.services.scheduling_agent.common import (
    FREE_BUSY,
    LEAD_MINUTES,
    SCHEDULE,
    actor_of,
    conflict,
    invalid,
    now,
    require,
    settings_row,
)


def _earliest(step: int) -> datetime:
    at = now().astimezone(UTC) + timedelta(minutes=LEAD_MINUTES)
    at = at.replace(second=0, microsecond=0)
    rem = at.minute % step
    return at + timedelta(minutes=(step - rem) % step)


def _slot_json(s: slots.SlotResult, unknown: tuple[uuid.UUID, ...]) -> dict[str, Any]:
    return {
        "start": s.start.isoformat(), "end": s.end.isoformat(), "all_known_free": s.all_known_free,
        "unknown_participants": [str(u) for u in unknown], "conflicts": [str(c) for c in s.conflicts],
    }  # fmt: skip


def offer_slots(offer: SchedulingSlotOffer) -> list[Slot]:
    return [
        Slot(
            start=datetime.fromisoformat(s["start"]), end=datetime.fromisoformat(s["end"]),
            all_known_free=bool(s.get("all_known_free")),
            unknown_participants=tuple(uuid.UUID(u) for u in s.get("unknown_participants") or ()),
        )
        for s in offer.slots or []
    ]  # fmt: skip


def proposal_out(offer: SchedulingSlotOffer) -> SlotProposalOut:
    return SlotProposalOut(
        proposal_ref=offer.id, slots=tuple(offer_slots(offer)),
        unknown_participants=tuple(offer.unknown_participants or ()), partial=offer.partial,
    )  # fmt: skip


async def engine_inputs(
    ctx: RequestContext,
    db: AsyncSession,
    req: SchedulingRequest,
    *,
    start: datetime,
    end: datetime,
    participants: list[uuid.UUID] | None = None,
) -> list[slots.Person]:
    return await availability.people(
        ctx, db, participants if participants is not None else list(req.participant_user_ids), start, end,
        exclude_interview_id=req.interview_id if req.kind == "reschedule" else None,
    )  # fmt: skip


async def _with_known_conflicts(
    db: AsyncSession, req: SchedulingRequest, people: list[slots.Person]
) -> list[slots.Person]:
    """Slots that already failed a booking re-check of this request are never offered again (free/busy answers are
    cached for a few minutes, so the conflict is remembered as a busy block)."""
    import dataclasses

    from app.models.scheduling_agent import SchedulingRequestEvent

    events = (
        await db.execute(
            select(SchedulingRequestEvent.detail).where(
                SchedulingRequestEvent.request_id == req.id, SchedulingRequestEvent.kind == "conflict"
            )
        )
    ).scalars()
    blocks: list[tuple[datetime, datetime, set[str]]] = []
    for detail in events:
        raw = (detail or {}).get("start")
        if not raw:
            continue
        begin = datetime.fromisoformat(str(raw))
        blocks.append(
            (
                begin,
                begin + timedelta(minutes=int(req.duration_minutes)),
                {str(u) for u in detail.get("busy") or []},
            )
        )
    if not blocks:
        return people
    out: list[slots.Person] = []
    for p in people:
        extra = tuple((a, b) for a, b, who in blocks if not who or str(p.user_id) in who)
        out.append(dataclasses.replace(p, busy=tuple(sorted((*p.busy, *extra)))) if extra else p)
    return out


async def compute_offer(
    ctx: RequestContext,
    db: AsyncSession,
    req: SchedulingRequest,
    *,
    max_slots: int | None = None,
    horizon_business_days: int | None = None,
) -> SchedulingSlotOffer:
    settings = await settings_row(db)
    if req.round >= settings.max_rounds:
        raise conflict(
            "max_rounds_reached", "The negotiation reached its maximum number of rounds", round=req.round
        )
    step = int(settings.slot_step_minutes)
    earliest = _earliest(step)
    business_tz = await availability.organiser_timezone(db, req.owner_user_id)
    horizon = int(horizon_business_days or settings.horizon_business_days)
    bh = await availability.business_holidays(
        db, req.owner_user_id, earliest.date(), (earliest + timedelta(days=horizon * 2 + 7)).date()
    )
    start, end = slots.horizon_window(earliest, business_tz, horizon, bh)
    people = await _with_known_conflicts(db, req, await engine_inputs(ctx, db, req, start=start, end=end))
    hours = dict(settings.candidate_hours or {})
    result = slots.compute(
        people=people,
        candidate=slots.CandidateSide(req.candidate_timezone, hours.get("start", "08:00"), hours.get("end", "21:00")),
        duration_minutes=int(req.duration_minutes), buffer_minutes=int(req.buffer_minutes), step_minutes=step,
        earliest=earliest, horizon_business_days=horizon, business_tz=business_tz, business_holidays=bh,
        max_slots=int(max_slots or settings.max_slots),
    )  # fmt: skip
    for old in (
        await db.execute(
            select(SchedulingSlotOffer).where(
                SchedulingSlotOffer.request_id == req.id, SchedulingSlotOffer.status == "draft"
            )
        )
    ).scalars():
        old.status = "superseded"
    offer = SchedulingSlotOffer(
        id=uuid.uuid4(), tenant_id=req.tenant_id, request_id=req.id, candidate_id=req.candidate_id,
        round=int(req.round) + 1, slots=[_slot_json(s, result.unknown_participants) for s in result.slots],
        unknown_participants=list(result.unknown_participants), partial=result.partial, status="draft",
        run_id=getattr(ctx, "agent_run_id", None),
    )  # fmt: skip
    db.add(offer)
    await db.flush()
    kind, actor_id = actor_of(ctx)
    history.record(
        db, req, "slots_computed", actor_kind=kind, actor_id=actor_id,
        detail={"offer_id": offer.id, "round": offer.round, "slots": len(result.slots), "partial": result.partial,
                "unknown": len(result.unknown_participants)},
    )  # fmt: skip
    await db.flush()
    return offer


async def propose(ctx: RequestContext, db: AsyncSession, req: SlotProposalIn) -> SlotProposalOut:
    """Propose interview slots from the participants' known availability."""
    require(ctx, FREE_BUSY, SCHEDULE)
    if req.max_slots < 1 or req.max_slots > 10:
        raise invalid("invalid_max_slots", "max_slots must be between 1 and 10")
    if req.horizon_business_days < 1 or req.horizon_business_days > 20:
        raise invalid("invalid_horizon", "horizon_business_days must be between 1 and 20")
    app = await requests.load_application(ctx, db, req.application_id)
    request = await requests.ensure_for_proposal(ctx, db, app, req)
    if (
        req.participant_user_ids
        and list(req.participant_user_ids) != list(request.participant_user_ids)
        and request.status == "open"
    ):
        request.participant_user_ids = list(dict.fromkeys(req.participant_user_ids))
    offer = await compute_offer(
        ctx, db, request, max_slots=req.max_slots, horizon_business_days=req.horizon_business_days
    )
    return proposal_out(offer)


async def check_times(
    ctx: RequestContext, db: AsyncSession, req: SchedulingRequest, windows: list[tuple[datetime, datetime]]
) -> tuple[list[slots.SlotResult], list[slots.SlotResult]]:
    """Free slots inside Candidate-proposed windows (``matches``) and the valid-but-busy ones."""
    settings = await settings_row(db)
    if not windows:
        return [], []
    start = min(w[0] for w in windows) - timedelta(hours=2)
    end = max(w[1] for w in windows) + timedelta(hours=2)
    people = await engine_inputs(ctx, db, req, start=start, end=end)
    return slots.match_windows(
        windows, people=people, duration_minutes=int(req.duration_minutes), buffer_minutes=int(req.buffer_minutes),
        step_minutes=int(settings.slot_step_minutes), earliest=now(),
    )  # fmt: skip


async def recheck(
    ctx: RequestContext, db: AsyncSession, req: SchedulingRequest, start: datetime, end: datetime
) -> dict[str, Any]:
    """Re-check one slot at booking time: ``{"ok", "hard_fail", "busy": [user ids], "unknown": [user ids]}``."""
    people = await engine_inputs(ctx, db, req, start=start - timedelta(hours=2), end=end + timedelta(hours=2))
    conflicts = slots.evaluate(people, start, end, candidate=None, buffer_minutes=int(req.buffer_minutes))
    return {
        "ok": conflicts == (),
        "hard_fail": conflicts is None,
        "busy": list(conflicts or ()),
        "unknown": [p.user_id for p in people if not p.known],
    }


__all__ = [
    "check_times",
    "compute_offer",
    "engine_inputs",
    "offer_slots",
    "proposal_out",
    "propose",
    "recheck",
]
