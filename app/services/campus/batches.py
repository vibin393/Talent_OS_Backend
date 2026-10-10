# ruff: noqa: E501
"""Interview batches and auto-slotting (Epic 43 story 43.4, FR-219).

A batch (Demand, date, start / end time, time zone, slot length, panels of interviewers with a room or video link)
generates its slots per panel. ``auto_slot`` fills free slots round-robin across panels (time order, then panel) with
``shortlisted`` registrations on that Demand that have no slot yet, in score order, and per filled slot schedules a
P1 interview (``interviews_service.schedule_interview``: participants = the panel, location = room or video link,
stage = the first interview stage of the Demand's plan -- seeded from the drive's interview template before the
first interview). The P1 invite email / ICS notifies the student; optional SMS through P2 messaging. Re-slotting
reschedules through P1 (``move_slot``); cancelling a batch cancels its interviews.

Campus Applications skip the client submission flow: before the first interview the Application moves
``applied -> shortlisted`` (the confirming User) and ``shortlisted -> submitted`` (system, the P1 table allows it) so
P1 can schedule it (deviation, recorded).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.core.events import emit
from app.models.campus import BatchSlot, CampusDrive, InterviewBatch, Registration
from app.schemas.campus import AutoSlotOut, BatchIn, BatchOut, ItemError, SlotOut
from app.services.campus import channels
from app.services.campus.common import (
    candidate_facts,
    conflict,
    demand_refs,
    drive_for,
    invalid,
    now,
    tenant_of,
)
from app.services.campus.domain_events import CampusSlotAssigned

MAX_SLOTS = 2000


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise invalid("invalid_timezone", "Unknown time zone", {"timezone": "Unknown time zone"}) from exc


def slot_times(day: Any, start: Any, end: Any, minutes: int, tz: str) -> list[tuple[datetime, datetime]]:
    """Slot ``[start, end)`` boundaries in UTC for one panel (whole slots only)."""
    zone = _zone(tz)
    cur = datetime.combine(day, start, tzinfo=zone)
    stop = datetime.combine(day, end, tzinfo=zone)
    out: list[tuple[datetime, datetime]] = []
    step = timedelta(minutes=minutes)
    while cur + step <= stop:
        out.append((cur.astimezone(UTC), (cur + step).astimezone(UTC)))
        cur += step
    return out


def round_robin(slots: list[Any], registrations: list[Any]) -> list[tuple[Any, Any]]:
    """Pair free slots (time, then panel order) with registrations (score order): consecutive students go to
    consecutive panels of the same time slot."""
    free = sorted((s for s in slots if s.registration_id is None), key=lambda s: (s.starts_at, s.panel_index))
    return list(zip(free, registrations, strict=False))


# ================================================================ read models
async def batch_out(db: AsyncSession, batch: InterviewBatch, *, with_slots: bool = True) -> BatchOut:
    slots = list((await db.execute(select(BatchSlot).where(BatchSlot.batch_id == batch.id).order_by(BatchSlot.starts_at, BatchSlot.panel_index))).scalars())
    reg_ids = [s.registration_id for s in slots if s.registration_id]
    cand = dict((await db.execute(select(Registration.id, Registration.candidate_id).where(Registration.id.in_(reg_ids)))).tuples().all()) if reg_ids else {}
    facts = await candidate_facts(db, list(cand.values()))
    refs = await demand_refs(db, [batch.demand_id])
    return BatchOut(
        id=batch.id, drive_id=batch.drive_id, demand_id=batch.demand_id, demand_title=refs.get(batch.demand_id, {}).get("title"),
        date=batch.day, start_time=batch.start_time, end_time=batch.end_time, timezone=batch.timezone,
        slot_minutes=batch.slot_minutes, panels=list(batch.panels or []), status=batch.status,  # type: ignore[arg-type]
        send_sms=batch.send_sms, slot_count=len(slots), filled_count=sum(1 for s in slots if s.registration_id),
        slots=[
            SlotOut(
                id=s.id, panel_index=s.panel_index, starts_at=s.starts_at, ends_at=s.ends_at, registration_id=s.registration_id,
                candidate_name=facts.get(cand.get(s.registration_id), (None, None))[0] if s.registration_id else None,  # type: ignore[arg-type]
                interview_id=s.interview_id,
            )
            for s in slots
        ]
        if with_slots
        else [],
    )


async def _batch(c: RequestContext, db: AsyncSession, batch_id: uuid.UUID, *, write: bool = False, lock: bool = False) -> tuple[InterviewBatch, CampusDrive]:
    stmt = select(InterviewBatch).where(InterviewBatch.id == batch_id)
    if lock:
        stmt = stmt.with_for_update()
    batch = (await db.execute(stmt)).scalar_one_or_none()
    if batch is None:
        raise NotFound("Interview batch not found")
    try:
        drive = await drive_for(c, db, batch.drive_id, write=write)
    except NotFound as exc:
        raise NotFound("Interview batch not found") from exc
    return batch, drive


async def list_batches(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> list[BatchOut]:
    drive = await drive_for(c, db, drive_id)
    rows = list((await db.execute(select(InterviewBatch).where(InterviewBatch.drive_id == drive.id).order_by(InterviewBatch.day, InterviewBatch.start_time))).scalars())
    return [await batch_out(db, b, with_slots=False) for b in rows]


async def get_batch(c: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> BatchOut:
    batch, _ = await _batch(c, db, batch_id)
    return await batch_out(db, batch)


async def create_batch(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: BatchIn) -> BatchOut:
    from app.models.identity import User
    from app.services.campus import drives

    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status not in ("registration_open", "in_progress"):
        raise conflict("drive_not_running", "Interview batches belong to a running drive", status=drive.status)
    if data.demand_id not in await drives.drive_demand_ids(db, drive.id):
        raise conflict("demand_not_on_drive", "Choose one of the drive's demands")
    users = {u for p in data.panels for u in p.interviewer_user_ids}
    active = set((await db.execute(select(User.id).where(User.id.in_(users), User.is_active.is_(True)))).scalars())
    if users - active:
        raise invalid("unknown_interviewer", "Every interviewer must be an active user", {"panels": "Unknown interviewer"})
    times = slot_times(data.date, data.start_time, data.end_time, data.slot_minutes, data.timezone)
    if not times:
        raise invalid("no_slots", "The window is shorter than one slot", {"slot_minutes": "Longer than the window"})
    if len(times) * len(data.panels) > MAX_SLOTS:
        raise invalid("too_many_slots", f"At most {MAX_SLOTS} slots per batch", {"slot_minutes": "Too many slots"})
    batch = InterviewBatch(
        id=uuid.uuid4(), tenant_id=tenant_of(db), drive_id=drive.id, demand_id=data.demand_id, day=data.date,
        start_time=data.start_time, end_time=data.end_time, timezone=data.timezone, slot_minutes=data.slot_minutes,
        panels=[p.model_dump(mode="json") for p in data.panels], status="draft", send_sms=data.send_sms,
    )
    db.add(batch)
    await db.flush()
    for panel_index in range(len(data.panels)):
        for starts_at, ends_at in times:
            db.add(BatchSlot(id=uuid.uuid4(), tenant_id=batch.tenant_id, batch_id=batch.id, panel_index=panel_index, starts_at=starts_at, ends_at=ends_at))
    await db.flush()
    await db.refresh(batch)
    return await batch_out(db, batch)


# ================================================================ P1 interview plumbing (shared with events)
async def prepare_application(c: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> None:
    """Move a campus / event Application to a stage P1 can interview in (see the module docstring)."""
    from app.models.pipeline import Application, PipelineStage
    from app.services import pipeline_service

    app = await db.get(Application, application_id)
    if app is None:
        raise NotFound("Application not found")
    stage = await db.get(PipelineStage, app.stage_id)
    key = stage.key if stage is not None else None
    kind = stage.kind if stage is not None else None
    if key in ("submitted", "interviewing"):
        return
    if key == "applied" or kind == "custom":
        await pipeline_service.transition_application(c, db, app.id, "shortlisted", "user", note="Campus shortlist")
        key = "shortlisted"
    if key == "shortlisted":
        await pipeline_service.transition_application(c, db, app.id, "submitted", "system", note="Campus interview (no client submission)")
        return
    raise conflict("application_not_interviewable", "This application cannot be interviewed", stage=key)


async def interview_stage(c: RequestContext, db: AsyncSession, demand_id: uuid.UUID, template_id: uuid.UUID | None) -> uuid.UUID:
    from app.schemas.interviews import ApplyTemplateIn
    from app.services import interviews_service

    plan = await interviews_service.get_plan(c, db, demand_id)
    if template_id is not None and not plan.has_interviews:
        plan = await interviews_service.apply_template(c, db, demand_id, ApplyTemplateIn(template_id=template_id))
    for stage in plan.stages:
        if stage.kind != "assessment":
            return stage.id
    raise conflict("no_interview_stage", "The demand's interview plan has no interview stage")


def local(at: datetime, tz: str) -> str:
    return at.astimezone(_zone(tz)).replace(tzinfo=None).isoformat(timespec="minutes")


def _participants(panel: dict[str, Any]) -> list[Any]:
    from app.schemas.interviews import ParticipantIn

    return [ParticipantIn(kind="interviewer_internal", user_id=uuid.UUID(str(u))) for u in panel.get("interviewer_user_ids") or []]


# ================================================================ auto-slot / move / cancel
async def auto_slot(c: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> AutoSlotOut:
    from app.models.pipeline import Application
    from app.schemas.interviews import InterviewCreate
    from app.services import interviews_service

    batch, drive = await _batch(c, db, batch_id, write=True, lock=True)
    if batch.status not in ("draft", "scheduled"):
        raise conflict("batch_closed", "This batch is closed", status=batch.status)
    slots = list((await db.execute(select(BatchSlot).where(BatchSlot.batch_id == batch.id).with_for_update())).scalars())
    slotted = select(BatchSlot.registration_id).join(InterviewBatch, InterviewBatch.id == BatchSlot.batch_id).where(
        BatchSlot.registration_id.is_not(None), InterviewBatch.status != "cancelled"
    )
    regs = list(
        (
            await db.execute(
                select(Registration)
                .join(Application, Application.id == Registration.application_id)
                .where(
                    Registration.drive_id == drive.id,
                    Registration.status == "shortlisted",
                    Application.demand_id == batch.demand_id,
                    Registration.id.not_in(slotted),
                )
                .order_by(Registration.score.desc().nulls_last(), Registration.shortlisted_at, Registration.id)
                .with_for_update(of=Registration)
            )
        ).scalars()
    )
    pairs = round_robin(slots, regs)
    errors: list[ItemError] = []
    done = 0
    stage_id: uuid.UUID | None = None
    if pairs:
        stage_id = await interview_stage(c, db, batch.demand_id, drive.interview_stage_template_id)
    for slot, reg in pairs:
        if slot.starts_at <= now():
            errors.append(ItemError(id=reg.id, code="slot_in_past"))
            continue
        panel = (batch.panels or [])[slot.panel_index]
        data = InterviewCreate(
            stage_id=stage_id,  # type: ignore[arg-type]
            start_local=local(slot.starts_at, batch.timezone),
            timezone=batch.timezone,
            duration_minutes=batch.slot_minutes,
            mode="video" if panel.get("video_link") else "in_person",
            meeting_url=panel.get("video_link"),
            location=panel.get("room"),
            participants=_participants(panel),
            send_invites=True,
        )
        try:
            async with db.begin_nested():
                await prepare_application(c, db, reg.application_id)  # type: ignore[arg-type]
                interview = await interviews_service.schedule_interview(c, db, reg.application_id, data)  # type: ignore[arg-type]
                slot.registration_id = reg.id
                slot.interview_id = interview.id
                await db.flush()
        except DomainError as exc:
            errors.append(ItemError(id=reg.id, code=exc.code, message=exc.message))
            continue
        done += 1
        emit(
            db,
            CampusSlotAssigned(
                tenant_id=batch.tenant_id, registration_id=reg.id, interview_id=interview.id,
                interviewer_user_ids=tuple(uuid.UUID(str(u)) for u in panel.get("interviewer_user_ids") or []),
            ),
        )
        if batch.send_sms:
            await channels.send(c, db, "sms", reg.candidate_id, demand_id=batch.demand_id)
    if done and batch.status == "draft":
        batch.status = "scheduled"
    await db.flush()
    audit.record("campus.batch_auto_slotted", "interview_batch", batch.id, session=db, slotted=done)
    return AutoSlotOut(batch=await batch_out(db, batch), slotted=done, unslotted=len(regs) - done, errors=errors)


async def move_slot(c: RequestContext, db: AsyncSession, batch_id: uuid.UUID, slot_id: uuid.UUID, to_slot_id: uuid.UUID) -> BatchOut:
    from app.models.interviews import Interview
    from app.schemas.interviews import InterviewReschedule
    from app.services import interviews_service

    batch, _ = await _batch(c, db, batch_id, write=True, lock=True)
    src = await db.get(BatchSlot, slot_id, with_for_update=True)
    dst = await db.get(BatchSlot, to_slot_id, with_for_update=True)
    if src is None or dst is None or src.batch_id != batch.id or dst.batch_id != batch.id:
        raise NotFound("Slot not found")
    if src.registration_id is None:
        raise conflict("slot_empty", "There is nobody in this slot")
    if dst.registration_id is not None:
        raise conflict("slot_taken", "The target slot is taken")
    if src.interview_id is not None:
        row = await db.get(Interview, src.interview_id)
        if row is not None and row.status == "scheduled":
            panel = (batch.panels or [])[dst.panel_index]
            await interviews_service.reschedule_interview(
                c, db, row.id,
                InterviewReschedule(
                    start_local=local(dst.starts_at, batch.timezone), timezone=batch.timezone, duration_minutes=batch.slot_minutes,
                    expected_version=row.version, participants=_participants(panel),
                    mode="video" if panel.get("video_link") else "in_person", meeting_url=panel.get("video_link"), location=panel.get("room"),
                ),
            )
    dst.registration_id, dst.interview_id = src.registration_id, src.interview_id
    src.registration_id, src.interview_id = None, None
    await db.flush()
    audit.record("campus.slot_moved", "interview_batch", batch.id, session=db)
    return await batch_out(db, batch)


async def cancel_batch(c: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> BatchOut:
    from app.models.interviews import Interview
    from app.services.interviews.scheduling import cancel_row

    batch, _ = await _batch(c, db, batch_id, write=True, lock=True)
    if batch.status == "cancelled":
        return await batch_out(db, batch)
    if batch.status == "completed":
        raise conflict("batch_closed", "This batch is completed")
    slots = list((await db.execute(select(BatchSlot).where(BatchSlot.batch_id == batch.id, BatchSlot.interview_id.is_not(None)))).scalars())
    for s in slots:
        row = await db.get(Interview, s.interview_id, with_for_update=True)
        if row is not None and row.status == "scheduled":
            await cancel_row(c, db, row, "Campus interview batch cancelled")
        s.registration_id = None
        s.interview_id = None
    batch.status = "cancelled"
    await db.flush()
    audit.record("campus.batch_cancelled", "interview_batch", batch.id, session=db)
    return await batch_out(db, batch)


async def slotted_count(db: AsyncSession, drive_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(BatchSlot)
                .join(InterviewBatch, InterviewBatch.id == BatchSlot.batch_id)
                .where(InterviewBatch.drive_id == drive_id, BatchSlot.registration_id.is_not(None))
            )
        ).scalar_one()
    )
