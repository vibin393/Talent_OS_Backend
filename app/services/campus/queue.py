# ruff: noqa: E501
"""Server-side interview queue of a recruitment event (Epic 43 story 43.5, FR-220, AD-15: no websocket).

* ``queue_view`` -- active entries in position order with ``eta = ceil(ahead / open stations) x median`` where the
  median is the rolling median of the last 10 completed interview durations (else the event's
  ``avg_interview_minutes``). The router adds ``ETag`` / ``Cache-Control: no-store``; views poll every <= 5 s.
* ``call_next`` -- ``SELECT ... WHERE status='waiting' ORDER BY position FOR UPDATE SKIP LOCKED LIMIT 1`` (matching the
  station's Demand when it has one): two stations calling at once never get the same entry. The Candidate is notified
  once (``notified_at``) through P2 messaging (SMS honours opt-outs; WhatsApp needs the opt-in) -- a failure is
  recorded in ``notify_result`` and never blocks the queue.
* ``start`` -- ``in_interview`` plus an immediate P1 interview (participants = the station's interviewers) so the quick
  scorecard is P1 FR-41; ``finish`` -- ``done | skipped | left``; ``requeue`` puts a skipped entry back at the end.
"""

import math
import statistics
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.core.events import emit
from app.models.campus import QUEUE_ACTIVE, EventCheckin, EventStation, QueueEntry, RecruitmentEvent
from app.schemas.campus import EventDashboardOut, QueueEntryOut, QueueOut
from app.services.campus import channels
from app.services.campus.common import candidate_facts, conflict, demand_refs, event_for, now, user_id_of
from app.services.campus.domain_events import QueueTurn

RUNNING = ("open", "live")
MEDIAN_WINDOW = 10


def estimate(ahead: int, open_stations: int, median_minutes: int) -> int | None:
    """``ceil(ahead / open stations) x median`` minutes (None when no station is open)."""
    if open_stations <= 0:
        return None
    return math.ceil(ahead / open_stations) * median_minutes


def rolling_median(durations: list[float], default: int) -> int:
    if not durations:
        return default
    return max(1, round(statistics.median(durations[:MEDIAN_WINDOW])))


async def _median(db: AsyncSession, event: RecruitmentEvent) -> int:
    rows = (
        await db.execute(
            select(QueueEntry.started_at, QueueEntry.completed_at)
            .where(QueueEntry.event_id == event.id, QueueEntry.status == "done", QueueEntry.started_at.is_not(None), QueueEntry.completed_at.is_not(None))
            .order_by(QueueEntry.completed_at.desc())
            .limit(MEDIAN_WINDOW)
        )
    ).all()
    return rolling_median([(b - a).total_seconds() / 60 for a, b in rows], event.avg_interview_minutes)


async def entry_outs(db: AsyncSession, rows: list[QueueEntry], *, eta: dict[uuid.UUID, tuple[int, int | None]] | None = None) -> list[QueueEntryOut]:
    facts = await candidate_facts(db, [r.candidate_id for r in rows])
    refs = await demand_refs(db, [r.demand_id for r in rows if r.demand_id])
    station_ids = [r.station_id for r in rows if r.station_id]
    labels = dict((await db.execute(select(EventStation.id, EventStation.label).where(EventStation.id.in_(station_ids)))).tuples().all()) if station_ids else {}
    out: list[QueueEntryOut] = []
    for r in rows:
        ahead, minutes = (eta or {}).get(r.id, (None, None))
        out.append(
            QueueEntryOut(
                id=r.id, position=r.position, candidate_id=r.candidate_id, candidate_name=facts.get(r.candidate_id, (None, None))[0],
                demand_id=r.demand_id, demand_title=refs.get(r.demand_id, {}).get("title") if r.demand_id else None,
                status=r.status, station_id=r.station_id, station_label=labels.get(r.station_id) if r.station_id else None,  # type: ignore[arg-type]
                application_id=r.application_id, interview_id=r.interview_id, ahead=ahead, eta_minutes=minutes,
                called_at=r.called_at, started_at=r.started_at, completed_at=r.completed_at, notified_at=r.notified_at,
                notify_result=r.notify_result,
            )
        )
    return out


async def queue_view(c: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> QueueOut:
    from app.services.campus.events import station_outs

    event = await event_for(c, db, event_id)
    rows = list(
        (await db.execute(select(QueueEntry).where(QueueEntry.event_id == event.id, QueueEntry.status.in_(QUEUE_ACTIVE)).order_by(QueueEntry.position))).scalars()
    )
    stations = await station_outs(db, event.id)
    open_count = sum(1 for s in stations if s.status == "open")
    median = await _median(db, event)
    eta: dict[uuid.UUID, tuple[int, int | None]] = {}
    ahead = 0
    for r in rows:
        if r.status == "waiting":
            eta[r.id] = (ahead, estimate(ahead, open_count, median))
            ahead += 1
    return QueueOut(
        event_id=event.id, status=event.status, open_stations=open_count, median_minutes=median,  # type: ignore[arg-type]
        waiting=ahead, entries=await entry_outs(db, rows, eta=eta), stations=stations, generated_at=now(),
    )


async def _station(db: AsyncSession, event: RecruitmentEvent, station_id: uuid.UUID, *, lock: bool = False) -> EventStation:
    s = await db.get(EventStation, station_id, with_for_update=lock)
    if s is None or s.event_id != event.id:
        raise NotFound("Station not found")
    return s


async def call_next(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, station_id: uuid.UUID) -> QueueEntryOut | None:
    event = await event_for(c, db, event_id)
    if event.status not in RUNNING:
        raise conflict("event_not_running", "The event is not running", status=event.status)
    station = await _station(db, event, station_id, lock=True)
    if station.status != "open":
        raise conflict("station_not_open", "This station is not open", status=station.status)
    busy = (
        await db.execute(select(QueueEntry.id).where(QueueEntry.station_id == station.id, QueueEntry.status.in_(("called", "in_interview"))).limit(1))
    ).first()
    if busy is not None:
        raise conflict("station_busy", "Finish the current candidate first", entry_id=str(busy[0]))
    stmt = select(QueueEntry).where(QueueEntry.event_id == event.id, QueueEntry.status == "waiting")
    if station.demand_id is not None:
        stmt = stmt.where((QueueEntry.demand_id == station.demand_id) | QueueEntry.demand_id.is_(None))
    entry = (await db.execute(stmt.order_by(QueueEntry.position).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if entry is None:
        return None
    entry.status = "called"
    entry.station_id = station.id
    entry.called_at = now()
    await db.flush()
    if entry.notified_at is None:
        entry.notify_result = await channels.send(c, db, event.notify_channel, entry.candidate_id, demand_id=entry.demand_id) if event.notify_channel != "none" else "in_app_only"
        entry.notified_at = now()
        emit(db, QueueTurn(tenant_id=event.tenant_id, event_id=event.id, entry_id=entry.id, candidate_id=entry.candidate_id))
        await db.flush()
    return (await entry_outs(db, [entry]))[0]


async def _entry(db: AsyncSession, event: RecruitmentEvent, entry_id: uuid.UUID) -> QueueEntry:
    e = await db.get(QueueEntry, entry_id, with_for_update=True)
    if e is None or e.event_id != event.id:
        raise NotFound("Queue entry not found")
    return e


async def start(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, entry_id: uuid.UUID) -> QueueEntryOut:
    from app.schemas.interviews import InterviewCreate, ParticipantIn
    from app.services import interviews_service
    from app.services.campus.batches import interview_stage, local, prepare_application

    event = await event_for(c, db, event_id)
    entry = await _entry(db, event, entry_id)
    if entry.status != "called":
        raise conflict("transition_not_allowed", "Call the candidate before starting", status=entry.status)
    station = await db.get(EventStation, entry.station_id) if entry.station_id else None
    at = now()
    if entry.application_id is not None and entry.interview_id is None:
        interviewers = list(station.interviewer_user_ids or []) if station is not None else []
        if not interviewers and user_id_of(c) is not None:
            interviewers = [user_id_of(c)]  # type: ignore[list-item]
        await prepare_application(c, db, entry.application_id)
        app_demand = entry.demand_id
        assert app_demand is not None
        interview = await interviews_service.schedule_interview(
            c, db, entry.application_id,
            InterviewCreate(
                stage_id=await interview_stage(c, db, app_demand, None), start_local=local(at, event.timezone), timezone=event.timezone,
                duration_minutes=event.avg_interview_minutes, mode="video" if event.kind == "virtual" else "in_person",
                meeting_url=event.video_link if event.kind == "virtual" else None,
                location=(f"{event.venue or event.name} - {station.label}" if station else event.venue)[:500] if (station or event.venue) else None,
                participants=[ParticipantIn(kind="interviewer_internal", user_id=u) for u in interviewers],
                send_invites=False, allow_past=True,
            ),
        )
        entry.interview_id = interview.id
    entry.status = "in_interview"
    entry.started_at = at
    await db.flush()
    return (await entry_outs(db, [entry]))[0]


FINISH_FROM: dict[str, tuple[str, ...]] = {
    "done": ("called", "in_interview"),
    "skipped": ("waiting", "called"),
    "left": ("waiting", "called", "in_interview"),
}


async def finish(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, entry_id: uuid.UUID, to: str) -> QueueEntryOut:
    from app.services import interviews_service

    event = await event_for(c, db, event_id)
    entry = await _entry(db, event, entry_id)
    if entry.status not in FINISH_FROM[to]:
        raise conflict("transition_not_allowed", f"A {entry.status.replace('_', ' ')} entry cannot become {to}", status=entry.status)
    at = now()
    if to == "done" and entry.started_at is None:
        entry.started_at = entry.called_at or at
    entry.status = to
    entry.completed_at = at
    if to == "left":
        checkin = await db.get(EventCheckin, entry.checkin_id)
        if checkin is not None:
            checkin.status = "left"
    await db.flush()
    if to == "done" and entry.interview_id is not None:
        try:
            async with db.begin_nested():
                await interviews_service.complete_interview(c, db, entry.interview_id)
        except DomainError:
            pass  # already completed / not scheduled: the scorecard flow is unaffected
    audit.record(f"campus.queue_{to}", "recruitment_event", event.id, session=db)
    return (await entry_outs(db, [entry]))[0]


async def requeue(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, entry_id: uuid.UUID) -> QueueEntryOut:
    from app.services.campus.events import next_position

    event = await event_for(c, db, event_id, lock=True)
    entry = await _entry(db, event, entry_id)
    if entry.status not in ("skipped", "called"):
        raise conflict("transition_not_allowed", "Only a skipped or called entry can go back to the queue", status=entry.status)
    entry.status = "waiting"
    entry.station_id = None
    entry.called_at = None
    entry.completed_at = None
    entry.position = await next_position(db, event.id)
    await db.flush()
    return (await entry_outs(db, [entry]))[0]


async def dashboard(c: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> EventDashboardOut:
    from app.models.pipeline import Application, PipelineStage

    event = await event_for(c, db, event_id)
    checkins = dict((await db.execute(select(EventCheckin.status, func.count()).where(EventCheckin.event_id == event.id).group_by(EventCheckin.status))).tuples().all())
    queue = dict((await db.execute(select(QueueEntry.status, func.count()).where(QueueEntry.event_id == event.id).group_by(QueueEntry.status))).tuples().all())
    waits = (
        await db.execute(
            select(func.avg(func.extract("epoch", QueueEntry.called_at - EventCheckin.checked_in_at)))
            .join(EventCheckin, EventCheckin.id == QueueEntry.checkin_id)
            .where(QueueEntry.event_id == event.id, QueueEntry.called_at.is_not(None), EventCheckin.checked_in_at.is_not(None))
        )
    ).scalar_one_or_none()
    outcomes: dict[str, Any] = dict(
        (
            await db.execute(
                select(PipelineStage.key, func.count())
                .select_from(QueueEntry)
                .join(Application, Application.id == QueueEntry.application_id)
                .join(PipelineStage, PipelineStage.id == Application.stage_id)
                .where(QueueEntry.event_id == event.id)
                .group_by(PipelineStage.key)
            )
        ).tuples().all()
    )
    return EventDashboardOut(
        event_id=event.id, registered=sum(int(v) for v in checkins.values()),
        checked_in=int(checkins.get("checked_in", 0)) + int(checkins.get("left", 0)),
        waiting=int(queue.get("waiting", 0)), called=int(queue.get("called", 0)), in_interview=int(queue.get("in_interview", 0)),
        interviewed=int(queue.get("done", 0)), skipped=int(queue.get("skipped", 0)), left=int(queue.get("left", 0)),
        average_wait_minutes=round(float(waits) / 60) if waits is not None else None,
        outcomes={str(k): int(v) for k, v in outcomes.items()},
    )
