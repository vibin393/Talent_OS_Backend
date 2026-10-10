# ruff: noqa: E501
"""Scheduling Agent settings, interviewer preferences and the request read models (Epic 59 A6)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.scheduling_agent import InterviewerSchedulingPrefs, SchedulingRequest, SchedulingSlotOffer
from app.schemas.scheduling_agent import (
    HistoryOut,
    OfferOut,
    OfferSlotOut,
    ParticipantRef,
    RequestDetailOut,
    RequestOut,
    SchedulingPrefsIn,
    SchedulingPrefsOut,
    SchedulingSettingsIn,
    SchedulingSettingsOut,
)
from app.services.scheduling_agent import availability, history, requests
from app.services.scheduling_agent.common import agent_on, invalid, settings_row, tenant_of, user_id_of


# ================================================================ settings
async def get_settings(db: AsyncSession) -> SchedulingSettingsOut:
    row = await settings_row(db)
    return SchedulingSettingsOut(
        max_slots=row.max_slots, horizon_business_days=row.horizon_business_days, slot_step_minutes=row.slot_step_minutes,
        default_buffer_minutes=row.default_buffer_minutes, candidate_hours=dict(row.candidate_hours or {}),
        max_rounds=row.max_rounds, link_days=row.link_days, reminder_offsets_minutes=list(row.reminder_offsets_minutes or []),
        reminder_channels=list(row.reminder_channels or []), scorecard_nudge_after_hours=row.scorecard_nudge_after_hours,
        scorecard_nudge_max=row.scorecard_nudge_max, default_working_hours=dict(row.default_working_hours or {}),
        whatsapp_template_id=row.whatsapp_template_id, agent_on=await agent_on(db),
    )  # fmt: skip


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: SchedulingSettingsIn
) -> SchedulingSettingsOut:
    row = await settings_row(db)
    values = data.model_dump(exclude_unset=True)
    if "candidate_hours" in values and values["candidate_hours"] is not None:
        hours = values["candidate_hours"]
        if hours["start"] >= hours["end"]:
            raise invalid("invalid_candidate_hours", "Candidate hours must end after they start")
    if "reminder_offsets_minutes" in values and values["reminder_offsets_minutes"] is not None:
        values["reminder_offsets_minutes"] = sorted(set(values["reminder_offsets_minutes"]), reverse=True)
    if "default_working_hours" in values and values["default_working_hours"] is not None:
        values["default_working_hours"] = {
            k: [list(i) for i in v] for k, v in values["default_working_hours"].items() if v
        }
    for key, value in values.items():
        if value is None and key not in ("whatsapp_template_id",):
            continue
        setattr(row, key, value)
    row.updated_by = user_id_of(ctx)
    await db.flush()
    audit.record(
        "scheduling_settings.updated", "scheduling_agent_settings", row.id, session=db, fields=sorted(values)
    )
    return await get_settings(db)


# ================================================================ preferences
async def get_prefs(db: AsyncSession, user_id: uuid.UUID) -> SchedulingPrefsOut:
    from app.services import entity_refs

    if await entity_refs.user(db, user_id) is None:
        raise NotFound("User not found")
    row = (await availability.prefs_map(db, [user_id])).get(user_id)
    connected = bool(await availability.calendar_connected(db, [user_id]))
    if row is None:
        tz = await availability.tenant_timezone(db)
        return SchedulingPrefsOut(
            user_id=user_id, timezone=tz, working_hours=await availability.default_working_hours(db),
            holiday_country=availability.country_for_zone(tz), holiday_region=None, max_interviews_per_day=None,
            buffer_minutes=None, is_default=True, calendar_connected=connected,
        )  # fmt: skip
    return SchedulingPrefsOut(
        user_id=user_id, timezone=row.timezone, working_hours=dict(row.working_hours or {}), holiday_country=row.holiday_country,
        holiday_region=row.holiday_region, max_interviews_per_day=row.max_interviews_per_day, buffer_minutes=row.buffer_minutes,
        is_default=False, calendar_connected=connected,
    )  # fmt: skip


async def put_prefs(
    ctx: RequestContext,  # noqa: ARG001 - audit reads the session actor
    db: AsyncSession,
    user_id: uuid.UUID,
    data: SchedulingPrefsIn,
) -> SchedulingPrefsOut:
    from app.services import entity_refs
    from app.services.interviews import timeutil

    if await entity_refs.user(db, user_id) is None:
        raise NotFound("User not found")
    timeutil.zone(data.timezone)  # 422 invalid_timezone
    hours = {k: [list(i) for i in v] for k, v in data.working_hours.model_dump().items() if v}
    row = (
        await db.execute(
            select(InterviewerSchedulingPrefs).where(InterviewerSchedulingPrefs.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = InterviewerSchedulingPrefs(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            user_id=user_id,
            timezone=data.timezone,
            working_hours=hours,
        )
        db.add(row)
    row.timezone, row.working_hours = data.timezone, hours
    row.holiday_country, row.holiday_region = data.holiday_country, data.holiday_region
    row.max_interviews_per_day, row.buffer_minutes = data.max_interviews_per_day, data.buffer_minutes
    await db.flush()
    audit.record("scheduling_prefs.updated", "interviewer_scheduling_prefs", row.id, session=db)
    return await get_prefs(db, user_id)


# ================================================================ read models
def _offer_out(o: SchedulingSlotOffer) -> OfferOut:
    return OfferOut(
        id=o.id, round=o.round, status=o.status, partial=o.partial, channel=o.channel,
        slots=[
            OfferSlotOut(
                index=i, start=datetime.fromisoformat(s["start"]), end=datetime.fromisoformat(s["end"]),
                all_known_free=bool(s.get("all_known_free")), unknown_participants=[uuid.UUID(u) for u in s.get("unknown_participants") or []],
            )
            for i, s in enumerate(o.slots or [])
        ],
        unknown_participants=list(o.unknown_participants or []), sent_at=o.sent_at, expires_at=o.expires_at,
        picked_index=o.picked_index, picked_at=o.picked_at, counter_windows=o.counter_windows, candidate_note=o.candidate_note,
    )  # fmt: skip


async def request_out(db: AsyncSession, req: SchedulingRequest) -> RequestOut:
    from app.models.interviews import InterviewStage

    await db.flush()
    await db.refresh(req)  # server-side updated_at / defaults after a change in this session
    stage = await db.get(InterviewStage, req.stage_id)
    settings = await settings_row(db)
    return RequestOut(
        id=req.id, human_id=req.human_id, application_id=req.application_id, candidate_id=req.candidate_id,
        demand_id=req.demand_id, stage_id=req.stage_id, stage_name=stage.name if stage else None, interview_id=req.interview_id,
        kind=req.kind, status=req.status, round=req.round, max_rounds=settings.max_rounds,  # type: ignore[arg-type]
        duration_minutes=req.duration_minutes, buffer_minutes=req.buffer_minutes,
        participant_user_ids=list(req.participant_user_ids or []), mode=req.mode, candidate_timezone=req.candidate_timezone,
        owner_user_id=req.owner_user_id, current_run_id=req.current_run_id, booked_interview_id=req.booked_interview_id,
        handoff_reason=req.handoff_reason, reschedule_reason=req.reschedule_reason, created_at=req.created_at,
        updated_at=req.updated_at,
    )  # fmt: skip


async def request_detail(db: AsyncSession, req: SchedulingRequest) -> RequestDetailOut:
    base = await request_out(db, req)
    ids = list(req.participant_user_ids or [])
    names = await availability.user_names(db, ids)
    prefs = await availability.prefs_map(db, ids)
    tenant_tz = await availability.tenant_timezone(db)
    connected = await availability.calendar_connected(db, ids)
    participants = [
        ParticipantRef(
            user_id=u,
            name=names.get(u),
            timezone=prefs[u].timezone if u in prefs else tenant_tz,
            calendar_connected=u in connected,
        )
        for u in ids
    ]
    offers = [_offer_out(o) for o in await requests.offers_of(db, req.id)]
    events = [
        HistoryOut(
            id=e.id,
            kind=e.kind,
            actor_kind=e.actor_kind,
            actor_id=e.actor_id,
            detail=dict(e.detail or {}),
            at=e.at,
        )
        for e in await history.list_for(db, req.id)
    ]
    return RequestDetailOut(**base.model_dump(), participants=participants, offers=offers, history=events)


async def participants_view(db: AsyncSession, req: SchedulingRequest) -> dict[str, Any]:
    """Tool view: internal interviewers (no emails) and the external count."""
    ids = list(req.participant_user_ids or [])
    names = await availability.user_names(db, ids)
    prefs = await availability.prefs_map(db, ids)
    tenant_tz = await availability.tenant_timezone(db)
    connected = await availability.calendar_connected(db, ids)
    default_hours = await availability.default_working_hours(db)
    return {
        "internal": [
            {
                "user_id": u, "name": names.get(u), "timezone": prefs[u].timezone if u in prefs else tenant_tz,
                "calendar_connected": u in connected, "working_hours": dict(prefs[u].working_hours) if u in prefs else default_hours,
            }
            for u in ids
        ],
        "external_count": len(req.external_participants or []),
    }  # fmt: skip


__all__ = [
    "get_prefs",
    "get_settings",
    "participants_view",
    "put_prefs",
    "request_detail",
    "request_out",
    "update_settings",
]
