# ruff: noqa: E501
"""Epic 59 eval seeds: an Application of the org's candidate in Submitted (default interview plan), the Scheduling Agent
enabled, the owner's working hours set, and a scheduling request in the state the case needs (created without a run).
Subject = the Application. Placeholders: ``{{request_id}}``, ``{{offer_id}}``, ``{{application_id}}``,
``{{candidate_id}}``, ``{{owner_id}}``, ``{{interview_id}}``, ``{{reminder_id}}``, ``{{window_start}}``,
``{{window_end}}``, ``{{random_id}}``.

The owner has no connected calendar here (availability "unknown"): slots are computed from working hours, holidays and
the candidate's hours. Conflicts are produced with working hours (a picked time outside them fails the re-check).

Variants: ``scheduling_draft`` (a draft offer), ``scheduling_partial`` (a partial draft offer), ``scheduling_picked``
(an open offer the candidate picked), ``scheduling_picked_conflict`` (the picked time is outside working hours now),
``scheduling_counter`` (a declined offer with a candidate window inside working hours), ``scheduling_counter_max``
(max rounds reached, window outside working hours), ``scheduling_booked`` (a booked interview), ``scheduling_reschedule``
(a booked interview + a picked reschedule offer), ``scheduling_reminder`` (a due reminder row), ``scheduling_nudge``
(a completed interview with a due scorecard nudge)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from evals.fixtures import register_async_builder

ALL_DAY = {d: [["00:00", "24:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
MORNING = {d: [["09:00", "10:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
TZ = "Asia/Kolkata"


def _at_local(days: int, hour: int) -> datetime:
    d = (datetime.now(UTC) + timedelta(days=days)).astimezone(ZoneInfo(TZ)).date()
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def _full_ctx(org: Any) -> Any:
    from app.core.context import RequestContext
    from app.core.permissions import CATALOG, Principal, Scope

    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type="user",
            id=org.admin.user_id,
            tenant_id=org.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        ),
    )


async def _base(org: Any, hours: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.database import tenant_session
    from app.models.scheduling_agent import InterviewerSchedulingPrefs
    from app.services import ai, pipeline_service
    from app.services.scheduling_agent.common import settings_row
    from tests.modules.demands.helpers import create_client, open_demand
    from tests.modules.pipeline.helpers import add_one, shortlist

    client_id = await create_client(org.admin)
    demand = await open_demand(org.admin, client_id)
    app_id = uuid.UUID(await add_one(org.admin, str(demand["id"]), str(org.candidate_id)))
    await shortlist(org.admin, str(app_id))
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agentic": True, "agent_scheduling": True})
        await pipeline_service.transition_application(_full_ctx(org), db, app_id, "submitted", "system")
        db.add(
            InterviewerSchedulingPrefs(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                user_id=org.admin.user_id,
                timezone=TZ,
                working_hours=hours or ALL_DAY,
                holiday_country="IN",
            )
        )
        settings = await settings_row(db)
        settings.candidate_hours = {"start": "00:00", "end": "24:00"}
        settings.default_buffer_minutes = 0
    return {"application_id": app_id, "demand_id": uuid.UUID(str(demand["id"]))}


async def _request(
    org: Any,
    app_id: uuid.UUID,
    *,
    kind: str = "new",
    interview_id: uuid.UUID | None = None,
    status: str = "open",
    round_: int = 0,
) -> Any:
    from sqlalchemy import select

    from app.core.database import tenant_session
    from app.core.sequences import next_human_id
    from app.models.interviews import InterviewStage
    from app.models.pipeline import Application
    from app.models.scheduling_agent import SchedulingRequest

    async with tenant_session(org.tenant_id) as db:
        app = await db.get(Application, app_id)
        stage = (
            await db.execute(
                select(InterviewStage)
                .where(InterviewStage.demand_id == app.demand_id)
                .order_by(InterviewStage.position)
                .limit(1)
            )
        ).scalar_one_or_none()
        if stage is None:
            from app.models.demands import Demand
            from app.services.interviews import plan

            stages = await plan.ensure_plan(db, await db.get(Demand, app.demand_id))
            stage = stages[0]
        req = SchedulingRequest(
            id=uuid.uuid4(), tenant_id=org.tenant_id, human_id=await next_human_id(db, "scheduling_request", "SCH"),
            application_id=app.id, candidate_id=app.candidate_id, demand_id=app.demand_id, stage_id=stage.id,
            interview_id=interview_id, kind=kind, status=status, round=round_, duration_minutes=60, buffer_minutes=0,
            participant_user_ids=[org.admin.user_id], external_participants=[], mode="video", candidate_timezone=TZ,
            owner_user_id=app.owner_id, requested_by_kind="user", requested_by_id=org.admin.user_id,
        )  # fmt: skip
        db.add(req)
        await db.flush()
        return req.id, stage.id


async def _offer(
    org: Any,
    req_id: uuid.UUID,
    slots: list[tuple[datetime, datetime]],
    *,
    status: str = "draft",
    partial: bool = False,
    round_: int = 1,
    picked: int | None = None,
    windows: list[tuple[datetime, datetime]] | None = None,
) -> uuid.UUID:
    from app.core.database import tenant_session
    from app.models.scheduling_agent import SchedulingRequest, SchedulingSlotOffer

    async with tenant_session(org.tenant_id) as db:
        req = await db.get(SchedulingRequest, req_id)
        offer = SchedulingSlotOffer(
            id=uuid.uuid4(), tenant_id=org.tenant_id, request_id=req_id, candidate_id=req.candidate_id, round=round_,
            slots=[{"start": a.isoformat(), "end": b.isoformat(), "all_known_free": not partial, "unknown_participants": [str(org.admin.user_id)], "conflicts": []} for a, b in slots],
            unknown_participants=[org.admin.user_id], partial=partial, status=status, picked_index=picked,
            pick_id=uuid.uuid4() if picked is not None else None, picked_at=datetime.now(UTC) if picked is not None else None,
            counter_windows=[{"start": a.isoformat(), "end": b.isoformat()} for a, b in windows] if windows else None,
            sent_at=datetime.now(UTC) if status != "draft" else None, expires_at=datetime.now(UTC) + timedelta(days=7) if status != "draft" else None,
        )  # fmt: skip
        db.add(offer)
        await db.flush()
        return offer.id


def _values(org: Any, app_id: uuid.UUID, **extra: Any) -> dict[str, str]:
    out = {
        "application_id": str(app_id), "candidate_id": str(org.candidate_id), "owner_id": str(org.admin.user_id),
        "random_id": str(uuid.uuid4()), "subject_type": "application", "subject_id": str(app_id),
        "subject_candidate_ids": str(org.candidate_id),
    }  # fmt: skip
    out.update({k: str(v) for k, v in extra.items()})
    return out


async def draft(org: Any) -> dict[str, str]:
    base = await _base(org)
    req_id, _ = await _request(org, base["application_id"])
    offer_id = await _offer(
        org, req_id, [(_at_local(3, 11), _at_local(3, 12)), (_at_local(4, 15), _at_local(4, 16))]
    )
    return _values(org, base["application_id"], request_id=req_id, offer_id=offer_id)


async def partial(org: Any) -> dict[str, str]:
    base = await _base(org)
    req_id, _ = await _request(org, base["application_id"])
    offer_id = await _offer(org, req_id, [(_at_local(3, 11), _at_local(3, 12))], partial=True)
    return _values(org, base["application_id"], request_id=req_id, offer_id=offer_id)


async def picked(org: Any) -> dict[str, str]:
    base = await _base(org)
    req_id, _ = await _request(org, base["application_id"], status="picked", round_=1)
    offer_id = await _offer(org, req_id, [(_at_local(3, 11), _at_local(3, 12))], status="picked", picked=0)
    return _values(org, base["application_id"], request_id=req_id, offer_id=offer_id)


async def picked_conflict(org: Any) -> dict[str, str]:
    base = await _base(org, MORNING)
    req_id, _ = await _request(org, base["application_id"], status="picked", round_=1)
    offer_id = await _offer(org, req_id, [(_at_local(3, 15), _at_local(3, 16))], status="picked", picked=0)
    return _values(org, base["application_id"], request_id=req_id, offer_id=offer_id)


async def counter(org: Any) -> dict[str, str]:
    base = await _base(org)
    req_id, _ = await _request(org, base["application_id"], status="counter_proposed", round_=1)
    start, end = _at_local(5, 15), _at_local(5, 17)
    offer_id = await _offer(
        org, req_id, [(_at_local(3, 11), _at_local(3, 12))], status="declined", windows=[(start, end)]
    )
    return _values(
        org,
        base["application_id"],
        request_id=req_id,
        offer_id=offer_id,
        window_start=start.isoformat(),
        window_end=(start + timedelta(hours=1)).isoformat(),
    )


async def counter_max(org: Any) -> dict[str, str]:
    base = await _base(org, MORNING)
    req_id, _ = await _request(org, base["application_id"], status="counter_proposed", round_=3)
    start, end = _at_local(5, 15), _at_local(5, 17)
    offer_id = await _offer(
        org,
        req_id,
        [(_at_local(3, 9), _at_local(3, 10))],
        status="declined",
        round_=3,
        windows=[(start, end)],
    )
    return _values(org, base["application_id"], request_id=req_id, offer_id=offer_id)


async def _interview(org: Any, app_id: uuid.UUID, stage_id: uuid.UUID, hours: int = 72) -> uuid.UUID:
    from app.core.database import tenant_session
    from app.schemas.interviews import InterviewCreate, ParticipantIn
    from app.services import interviews_service

    start = (
        (datetime.now(UTC) + timedelta(hours=hours))
        .astimezone(ZoneInfo(TZ))
        .replace(minute=0, second=0, microsecond=0, tzinfo=None)
    )
    async with tenant_session(org.tenant_id) as db:
        out = await interviews_service.schedule_interview(_full_ctx(org), db, app_id, InterviewCreate(
            stage_id=stage_id, start_local=start.isoformat(timespec="minutes"), timezone=TZ, duration_minutes=60, mode="video",
            meeting_url="https://meet.example.com/x", participants=[ParticipantIn(kind="interviewer_internal", user_id=org.admin.user_id)],
        ))  # fmt: skip
        return out.id


async def booked(org: Any) -> dict[str, str]:
    from app.core.database import tenant_session
    from app.models.scheduling_agent import SchedulingRequest

    base = await _base(org)
    req_id, stage_id = await _request(org, base["application_id"], status="picked", round_=1)
    offer_id = await _offer(org, req_id, [(_at_local(3, 11), _at_local(3, 12))], status="picked", picked=0)
    interview_id = await _interview(org, base["application_id"], stage_id)
    async with tenant_session(org.tenant_id) as db:
        req = await db.get(SchedulingRequest, req_id)
        req.status, req.booked_interview_id = "booked", interview_id
    return _values(
        org, base["application_id"], request_id=req_id, offer_id=offer_id, interview_id=interview_id
    )


async def reschedule(org: Any) -> dict[str, str]:
    base = await _base(org)
    _, stage_id = await _request(org, base["application_id"], status="cancelled")
    interview_id = await _interview(org, base["application_id"], stage_id)
    req_id, _ = await _request(
        org, base["application_id"], kind="reschedule", interview_id=interview_id, status="picked", round_=1
    )
    offer_id = await _offer(org, req_id, [(_at_local(6, 11), _at_local(6, 12))], status="picked", picked=0)
    return _values(
        org, base["application_id"], request_id=req_id, offer_id=offer_id, interview_id=interview_id
    )


async def reminder(org: Any) -> dict[str, str]:
    from sqlalchemy import select

    from app.core.database import tenant_session
    from app.models.scheduling_agent import SchedulingReminder

    base = await _base(org)
    _, stage_id = await _request(org, base["application_id"], status="cancelled")
    interview_id = await _interview(org, base["application_id"], stage_id, hours=30)
    async with tenant_session(org.tenant_id) as db:
        from app.services.scheduling_agent import reminders

        await reminders.on_scheduled(db, interview_id)
        row = (
            await db.execute(
                select(SchedulingReminder)
                .where(
                    SchedulingReminder.interview_id == interview_id, SchedulingReminder.offset_minutes == 1440
                )
                .order_by(SchedulingReminder.candidate_id.nulls_first())
                .limit(1)
            )
        ).scalar_one()
        row.due_at = datetime.now(UTC) - timedelta(minutes=1)
        rid = row.id
    return _values(org, base["application_id"], interview_id=interview_id, reminder_id=rid)


async def nudge(org: Any) -> dict[str, str]:
    from sqlalchemy import select

    from app.core.database import tenant_session
    from app.models.interviews import Interview, InterviewParticipant
    from app.models.scheduling_agent import SchedulingReminder

    base = await _base(org)
    _, stage_id = await _request(org, base["application_id"], status="cancelled")
    interview_id = await _interview(org, base["application_id"], stage_id, hours=30)
    async with tenant_session(org.tenant_id) as db:
        row = await db.get(Interview, interview_id)
        row.status = "completed"
        p = (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == interview_id,
                    InterviewParticipant.kind == "interviewer_internal",
                )
            )
        ).scalar_one()
        rem = SchedulingReminder(
            id=uuid.uuid4(), tenant_id=org.tenant_id, interview_id=interview_id, participant_id=p.id, candidate_id=None,
            kind="scorecard_nudge", offset_minutes=1440, sequence=row.sequence, due_at=datetime.now(UTC) - timedelta(minutes=1), status="due",
        )  # fmt: skip
        db.add(rem)
        await db.flush()
        rid = rem.id
    return _values(org, base["application_id"], interview_id=interview_id, reminder_id=rid)


for _name, _fn in (
    ("scheduling_draft", draft), ("scheduling_partial", partial), ("scheduling_picked", picked),
    ("scheduling_picked_conflict", picked_conflict), ("scheduling_counter", counter), ("scheduling_counter_max", counter_max),
    ("scheduling_booked", booked), ("scheduling_reschedule", reschedule), ("scheduling_reminder", reminder),
    ("scheduling_nudge", nudge),
):  # fmt: skip
    register_async_builder(_name, _fn)
