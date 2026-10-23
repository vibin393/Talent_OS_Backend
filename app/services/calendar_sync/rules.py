# ruff: file-ignore[line-too-long]
"""Change handling and conflict rules (Epic 72 stories 72.1 / 72.2, spec A5). Deterministic; echoes first.

Talent OS is the source of truth: inbound data changes only ``interview_participants.response_status``. A move or a
decline becomes a reschedule request (the Scheduling Agent when on, else a Task); with policy ``restore`` the moved
event is put back to the Talent OS time (at most ``restore_limit_per_hour`` times per hour, then ``conflict`` + a Task);
a deletion becomes a cancellation request (Task) -- never a cancellation (★: cancelling stays a person's decision).
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.calendar_sync import CalendarChangeDetected, InterviewParticipantResponded
from app.models.calendar_sync import CalendarEventLink, CalendarSyncSettings
from app.models.interviews import RESPONSE_STATUSES, Interview, InterviewParticipant
from app.services.calendar_sync.common import now, open_task, system_ctx, tenant_of, user_name
from app.services.calendar_sync.echo import is_deleted, is_echo, same_time
from app.services.connections.provider import EventChange

RESCHEDULE_KINDS = ("interviewer_internal", "interviewer_external", "candidate")
RESPONSE_LABELS = {"accepted": "accepted", "declined": "declined", "tentative": "tentatively accepted"}

# Spy hook for the ★ runtime assertion: inbound data never reaches a P1 cancel / reschedule service.
_restore_calls: list[uuid.UUID] = []


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="minutes") if value else "?"


async def _participants(db: AsyncSession, interview_id: uuid.UUID) -> list[InterviewParticipant]:
    return list(
        (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == interview_id, InterviewParticipant.removed_at.is_(None)
                )
            )
        ).scalars()
    )


async def _owner(db: AsyncSession, row: Interview) -> uuid.UUID | None:
    from app.models.pipeline import Application

    app = await db.get(Application, row.application_id)
    return app.owner_id if app is not None else row.organizer_user_id


async def request_reschedule(
    db: AsyncSession,
    row: Interview,
    *,
    participant: InterviewParticipant | None,
    reason: str,
    proposed: tuple[datetime | None, datetime | None] = (None, None),
) -> str:
    """Ask the Scheduling Agent (system actor) or, when it is off, the Application owner (Task). Returns
    ``agent`` / ``task``."""
    from app.services import scheduling_agent_service

    by = "candidate" if participant is not None and participant.kind == "candidate" else "interviewer"
    try:
        async with db.begin_nested():
            req = await scheduling_agent_service.request_reschedule_system(
                system_ctx(row.tenant_id), db, row.id, by=by, participant_id=participant.id if participant else None,
                reason=reason, proposed_start=proposed[0], proposed_end=proposed[1],
            )  # fmt: skip
    except Exception:
        logger.exception("calendar sync: reschedule request failed (interview={})", row.id)
        req = None
    if req is not None:
        return "agent"
    name = participant.name or participant.email if participant is not None else await user_name(db, row.organizer_user_id)
    notes = [f"A calendar change asks to reschedule this interview ({reason})."]
    if proposed[0] is not None:
        notes.append(f"Proposed time (UTC): {_iso(proposed[0])} - {_iso(proposed[1])}.")
    notes.append(f"The interview is still at {_iso(row.starts_at)} (UTC). Choose \"Reschedule\" or \"Keep current time\".")
    await open_task(
        db, owner_id=await _owner(db, row), title=f"Reschedule requested from {name}'s calendar",
        notes="\n".join(notes), entity_id=row.id,
    )  # fmt: skip
    return "task"


async def _apply_responses(db: AsyncSession, row: Interview, link: CalendarEventLink, change: EventChange) -> list[InterviewParticipant]:
    """Update ``response_status`` from the attendee list; returns the participants who newly declined."""
    parts = await _participants(db, row.id)
    by_email = {p.email.lower(): p for p in parts}
    own = next((p for p in parts if p.id == link.participant_id), None) if link.role == "attendee" else None
    declined: list[InterviewParticipant] = []
    for resp in change.attendees:
        p = own if (link.role == "attendee" and resp.is_self) else by_email.get(resp.email.lower())
        if p is None or (link.role == "attendee" and p is not own):
            continue
        value = resp.response if resp.response in RESPONSE_STATUSES else "needs_action"
        if p.response_status == value:
            continue
        p.response_status = value
        emit(db, InterviewParticipantResponded(tenant_id=row.tenant_id, interview_id=row.id, participant_id=p.id, response=value))
        if value == "declined" and p.kind in RESCHEDULE_KINDS:
            declined.append(p)
    await db.flush()
    return declined


async def _restore(db: AsyncSession, row: Interview, link: CalendarEventLink, settings: CalendarSyncSettings) -> None:
    from app.services import interviews_service

    at = now()
    recent = [t for t in (link.restore_count_window or []) if datetime.fromisoformat(t) > at - timedelta(hours=1)]
    if len(recent) >= settings.restore_limit_per_hour:
        link.state = "conflict"
        link.restore_count_window = recent[-3:]
        await db.flush()
        name = await user_name(db, row.organizer_user_id)
        await open_task(
            db, owner_id=await _owner(db, row), title="Calendar keeps moving this interview",
            notes=f"The calendar event keeps moving. We stopped restoring it — please check with {name}.", entity_id=row.id,
        )  # fmt: skip
        return
    recent.append(at.isoformat())
    link.restore_count_window = recent[-3:]
    link_id = link.id
    _restore_calls.append(row.id)
    event_id = await interviews_service.resync_event(db, row.id, reason="calendar_restore", sequence=link.tos_sequence + 1)
    restored = await db.get(CalendarEventLink, link_id)
    if restored is not None and event_id is not None:
        restored.state = "restoring"
        restored.restore_count_window = recent[-3:]
    await db.flush()


async def apply(db: AsyncSession, link: CalendarEventLink, change: EventChange, settings: CalendarSyncSettings) -> str:
    """Apply one change of a linked event; returns what happened (for tests / logs, never event content)."""
    row = await db.get(Interview, link.interview_id)
    if row is None:
        return "ignored"
    if link.state == "detached":
        return "detached"
    link.last_inbound_at = now()
    if row.status != "scheduled":
        link.state = "detached"
        await db.flush()
        return "detached"
    deleted = is_deleted(change)
    if is_echo(link, change, row.starts_at, row.ends_at):
        # our own write (or an unchanged event): only the responses may legitimately differ
        echoed = await _apply_responses(db, row, link, change)
        if link.state in ("restoring", "moved"):
            link.state = "in_sync"
        link.seen_start, link.seen_end, link.seen_status = change.start_utc, change.end_utc, change.status
        await db.flush()
        for p in echoed:
            emit(db, CalendarChangeDetected(tenant_id=row.tenant_id, interview_id=row.id, link_id=link.id, kind="declined", participant_id=p.id))
            await request_reschedule(db, row, participant=p, reason="calendar_declined")
        return "declined" if echoed else "echo"
    declined = await _apply_responses(db, row, link, change)
    outcome = "responses"
    if link.role == "attendee":
        if deleted and link.state != "deleted":
            link.state = "deleted"
            own = await db.get(InterviewParticipant, link.participant_id) if link.participant_id else None
            if own is not None and own.response_status != "declined":
                own.response_status = "declined"
                emit(db, InterviewParticipantResponded(tenant_id=row.tenant_id, interview_id=row.id, participant_id=own.id, response="declined"))
                if own.kind in RESCHEDULE_KINDS:
                    declined.append(own)
        link.seen_status = change.status
    for p in declined:
        emit(db, CalendarChangeDetected(tenant_id=row.tenant_id, interview_id=row.id, link_id=link.id, kind="declined", participant_id=p.id))
        await request_reschedule(db, row, participant=p, reason="calendar_declined")
        outcome = "declined"
    if link.role != "organizer":
        await db.flush()
        return outcome
    if deleted:
        if link.state != "deleted":
            link.state, link.seen_status = "deleted", "deleted"
            await db.flush()
            emit(db, CalendarChangeDetected(tenant_id=row.tenant_id, interview_id=row.id, link_id=link.id, kind="deleted"))
            name = await user_name(db, row.organizer_user_id)
            await open_task(
                db, owner_id=await _owner(db, row), title=f"Interview event removed from {name}'s calendar",
                notes=(
                    "The calendar event was removed. The interview is still scheduled.\n"
                    "Choose: \"Re-create the calendar event\", \"Cancel the interview\" or \"Dismiss\" on the interview."
                ),
                entity_id=row.id,
            )  # fmt: skip
            from app.services.scheduling_agent_service import request_reschedule_system

            try:
                async with db.begin_nested():
                    await request_reschedule_system(
                        system_ctx(tenant_of(db)), db, row.id, by="interviewer", participant_id=None, reason="calendar_deleted"
                    )
            except Exception:
                logger.exception("calendar sync: agent wake failed (interview={})", row.id)
        return "deleted"
    moved = change.start_utc is not None and not (
        same_time(change.start_utc, row.starts_at) and same_time(change.end_utc, row.ends_at)
    )
    link.seen_start, link.seen_end, link.seen_status = change.start_utc, change.end_utc, change.status
    if not moved:
        if link.state in ("moved", "restoring"):
            link.state = "in_sync"
        await db.flush()
        return outcome
    if link.state == "conflict":
        await db.flush()
        return "conflict"
    link.state = "moved"
    await db.flush()
    emit(db, CalendarChangeDetected(tenant_id=row.tenant_id, interview_id=row.id, link_id=link.id, kind="moved"))
    organiser = next((p for p in await _participants(db, row.id) if p.user_id == row.organizer_user_id), None)
    await request_reschedule(db, row, participant=organiser, reason="calendar_moved", proposed=(change.start_utc, change.end_utc))
    if settings.move_policy == "restore":
        await _restore(db, row, link, settings)
        refreshed = await db.get(CalendarEventLink, link.id)
        return "conflict" if refreshed is not None and refreshed.state == "conflict" else "restored"
    return "moved"


async def keep_talent_os_time(db: AsyncSession, interview_id: uuid.UUID) -> Any:
    """``keep_as_proposal``: the recruiter puts the event back once (no limit check)."""
    from app.services import interviews_service

    links = [
        lk for lk in (await db.execute(select(CalendarEventLink).where(CalendarEventLink.interview_id == interview_id))).scalars()
        if lk.role == "organizer" and lk.state in ("moved", "conflict")
    ]
    for lk in links:
        await interviews_service.resync_event(db, interview_id, reason="keep_talent_os_time", sequence=lk.tos_sequence + 1)
        again = await db.get(CalendarEventLink, lk.id)
        if again is not None:
            again.state, again.restore_count_window = "in_sync", []
    await db.flush()
    return len(links)
