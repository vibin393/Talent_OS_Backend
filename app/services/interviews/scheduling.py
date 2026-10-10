"""Interviews and participants (7.2, FR-40, AD-14).

* ``schedule_interview`` -- the application must be in Submitted / Interviewing (409
  ``application_not_interviewable``) and the stage in the demand's plan (409 ``stage_not_in_plan``). Times come
  in as ``start_local`` + IANA ``timezone`` and are stored in UTC. The candidate is always added from the
  candidate record (409 ``candidate_email_missing`` unless ``send_invites=false``). The first interview of a
  Submitted application moves it to Interviewing (actor ``system``). ``InterviewScheduled`` -> invites task.
* ``reschedule_interview`` -- bumps ``sequence`` (same UID), resets reminders, optionally replaces the
  participants (removed ones get a CANCEL); stale ``expected_version`` -> 409 ``version_conflict``.
* ``cancel_interview`` -- CANCEL to everyone, open external scorecard links revoked.
* Only ``scheduled`` interviews can be rescheduled / cancelled (409 ``interview_not_scheduled``).
* ``mark_no_show`` / ``complete_interview`` (also ``complete_elapsed`` from the beat task, actor ``system``).

Visibility: participants and the organiser always see their interview; others need ``interview:read`` and
read access to the application (scoped through its demand, Epic 6).
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams
from app.core.permissions import check
from app.core.sequences import next_human_id
from app.events.interviews import (
    InterviewCancelled,
    InterviewCompleted,
    InterviewRescheduled,
    InterviewScheduled,
)
from app.models.interviews import Interview, InterviewKit, InterviewParticipant, Scorecard
from app.schemas.interviews import (
    CancelIn,
    InterviewCreate,
    InterviewOut,
    InterviewReschedule,
    ParticipantIn,
    ParticipantOut,
)
from app.services.interviews import configuration, plan, ports, timeutil
from app.services.interviews.common import (
    READ,
    SCHEDULE,
    acting_as,
    conflict,
    now,
    offsets,
    principal_of,
    settings_row,
    tenant_of,
    user_id_of,
)

INTERVIEWABLE_STAGES = ("submitted", "interviewing")
SEQUENCE_KEY = "interview"
SCORECARD_PURPOSE = "interview_scorecard"
PARTICIPANT_SUBJECT = "interview_participant"
INTERVIEWER_KINDS = ("interviewer_internal", "interviewer_external")


# ================================================================ access
async def _is_participant(db: AsyncSession, interview: Interview, user_id: uuid.UUID | None) -> bool:
    if user_id is None:
        return False
    if interview.organizer_user_id == user_id:
        return True
    row = await db.execute(
        select(InterviewParticipant.id).where(
            InterviewParticipant.interview_id == interview.id,
            InterviewParticipant.user_id == user_id,
            InterviewParticipant.removed_at.is_(None),
        )
    )
    return row.first() is not None


async def interview_for(
    ctx: RequestContext,
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    manage: bool = False,
    for_update: bool = False,
) -> Interview:
    """Readable interview (404 otherwise); ``manage`` additionally needs ``interview:schedule`` on it (403)."""
    principal = principal_of(ctx)
    stmt = select(Interview).where(Interview.id == interview_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Interview not found")
    if principal.type != "user":
        return row
    participant = await _is_participant(db, row, principal.id)
    if manage:
        if not participant and READ not in principal.permissions:
            raise NotFound("Interview not found")
        try:
            await ports.application_for(ctx, db, row.application_id)
        except NotFound as exc:
            raise NotFound("Interview not found") from exc
        check(principal, SCHEDULE)
        return row
    if participant:
        return row
    if READ not in principal.permissions:
        raise NotFound("Interview not found")
    try:
        await ports.application_for(ctx, db, row.application_id)
    except DomainError as exc:
        raise NotFound("Interview not found") from exc
    return row


# ================================================================ output
async def participants_of(
    db: AsyncSession, interview_ids: Sequence[uuid.UUID], *, include_removed: bool = False
) -> dict[uuid.UUID, list[InterviewParticipant]]:
    if not interview_ids:
        return {}
    stmt = select(InterviewParticipant).where(InterviewParticipant.interview_id.in_(list(interview_ids)))
    if not include_removed:
        stmt = stmt.where(InterviewParticipant.removed_at.is_(None))
    out: dict[uuid.UUID, list[InterviewParticipant]] = {}
    for p in (
        await db.execute(stmt.order_by(InterviewParticipant.created_at, InterviewParticipant.id))
    ).scalars():
        out.setdefault(p.interview_id, []).append(p)
    return out


async def scorecard_statuses(db: AsyncSession, interview_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """participant_id -> scorecard status."""
    if not interview_ids:
        return {}
    rows = await db.execute(
        select(Scorecard.participant_id, Scorecard.status).where(
            Scorecard.interview_id.in_(list(interview_ids))
        )
    )
    return dict(rows.tuples().all())


async def interview_outs(db: AsyncSession, rows: Sequence[Interview]) -> list[InterviewOut]:
    items = list(rows)
    if not items:
        return []
    ids = [r.id for r in items]
    parts = await participants_of(db, ids)
    statuses = await scorecard_statuses(db, ids)
    titles = await ports.demand_titles(db, list({r.demand_id for r in items}))
    names = await ports.candidate_names(db, list({r.candidate_id for r in items}))
    out: list[InterviewOut] = []
    for r in items:
        plist = parts.get(r.id, [])
        required = [p for p in plist if p.scorecard_required]
        out.append(
            InterviewOut(
                id=r.id,
                human_id=r.human_id,
                application_id=r.application_id,
                demand_id=r.demand_id,
                demand_title=titles.get(r.demand_id),
                candidate_id=r.candidate_id,
                candidate_name=names.get(r.candidate_id),
                stage_id=r.stage_id,
                stage_name=r.stage_name,
                round=r.round,
                starts_at=r.starts_at,
                ends_at=r.ends_at,
                duration_minutes=int((r.ends_at - r.starts_at).total_seconds() // 60),
                timezone=r.timezone,
                start_local=timeutil.local_iso(r.starts_at, r.timezone),
                display_time=timeutil.render_for(r.starts_at, r.timezone),
                mode=r.mode,  # type: ignore[arg-type]
                meeting_url=r.meeting_url,
                location=r.location,
                notes=r.notes,
                status=r.status,  # type: ignore[arg-type]
                sequence=r.sequence,
                organizer_user_id=r.organizer_user_id,
                provider=r.provider,
                create_online_meeting=r.create_online_meeting,
                cancelled_reason=r.cancelled_reason,
                version=r.version,
                participants=[
                    ParticipantOut(
                        id=p.id,
                        kind=p.kind,  # type: ignore[arg-type]
                        user_id=p.user_id,
                        email=p.email,
                        name=p.name,
                        timezone=p.timezone,
                        local_time=timeutil.render_for(r.starts_at, p.timezone),
                        response_status=p.response_status,
                        scorecard_required=p.scorecard_required,
                        scorecard_status=statuses.get(p.id),  # type: ignore[arg-type]
                    )
                    for p in plist
                ],
                scorecards_submitted=sum(1 for p in required if statuses.get(p.id) == "submitted"),
                scorecards_required=len(required),
                created_at=r.created_at,
            )
        )
    return out


async def interview_out(db: AsyncSession, row: Interview) -> InterviewOut:
    return (await interview_outs(db, [row]))[0]


# ================================================================ helpers
def _duration(value: int | None, default: int) -> timedelta:
    minutes = value if value is not None else default
    if not 15 <= int(minutes) <= 480:
        raise ValidationFailed("Duration must be between 15 and 480 minutes", code="invalid_duration")
    return timedelta(minutes=int(minutes))


def _tz_or(value: str | None, fallback: str) -> str:
    if value:
        timeutil.zone(value)  # 422 invalid_timezone
        return value
    return fallback


def initial_reminder_state(starts_at: datetime, offset_list: Iterable[int], at: datetime) -> dict[str, Any]:
    """Offsets already passed when (re)scheduling are ``skipped`` so they are never sent."""
    return {str(o): "skipped" for o in offset_list if starts_at - timedelta(minutes=o) <= at}


async def _resolve_participants(
    db: AsyncSession,
    items: Sequence[ParticipantIn],
    *,
    interview_tz: str,
    tenant_tz: str,
    candidate_email: str | None,
) -> list[dict[str, Any]]:
    user_ids = [p.user_id for p in items if p.user_id is not None]
    directory = await ports.users(db, user_ids)
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in items:
        if p.kind == "interviewer_external" and p.user_id is not None:
            raise ValidationFailed("External interviewers are given by email", code="invalid_participant")
        if p.user_id is not None:
            info = directory.get(p.user_id)
            if info is None or not info.is_active:
                raise ValidationFailed(
                    "Unknown or inactive user",
                    code="invalid_participant",
                    details={"user_id": str(p.user_id)},
                )
            email, name, default_tz = info.email.lower(), info.name, tenant_tz
        else:
            if p.kind == "interviewer_internal":
                raise ValidationFailed("Internal interviewers need a user_id", code="invalid_participant")
            if not p.email:
                raise ValidationFailed("External participants need an email", code="invalid_participant")
            email, name, default_tz = p.email.lower(), p.name or None, interview_tz
        if email in seen or (candidate_email and email == candidate_email):
            raise ValidationFailed(
                "A participant appears twice", code="duplicate_participant", details={"email": email}
            )
        seen.add(email)
        resolved.append(
            {
                "kind": p.kind,
                "user_id": p.user_id,
                "email": email,
                "name": name,
                "timezone": _tz_or(p.timezone, default_tz),
                "scorecard_required": p.scorecard_required
                if p.scorecard_required is not None
                else p.kind in INTERVIEWER_KINDS,
            }
        )
    if not any(r["kind"] in INTERVIEWER_KINDS for r in resolved):
        raise ValidationFailed("At least one interviewer is required", code="interviewer_required")
    return resolved


def _participant(interview: Interview, spec: dict[str, Any]) -> InterviewParticipant:
    return InterviewParticipant(
        id=uuid.uuid4(),
        tenant_id=interview.tenant_id,
        interview_id=interview.id,
        kind=spec["kind"],
        user_id=spec.get("user_id"),
        email=spec["email"],
        name=spec.get("name"),
        timezone=spec["timezone"],
        response_status="needs_action",
        scorecard_required=bool(spec.get("scorecard_required")),
    )


async def _kit_snapshot(db: AsyncSession, kit_id: uuid.UUID | None, rating_scale: int) -> dict[str, Any]:
    kit = await db.get(InterviewKit, kit_id) if kit_id else None
    return configuration.kit_snapshot(kit, rating_scale)


# ================================================================ schedule
async def schedule_interview(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, data: InterviewCreate
) -> InterviewOut:
    principal = principal_of(ctx)
    app = await ports.application_for(ctx, db, application_id, for_update=True)
    check(principal, SCHEDULE)
    key = await ports.stage_key(db, app.stage_id)
    if key not in INTERVIEWABLE_STAGES:
        raise conflict(
            "application_not_interviewable",
            "Interviews can be scheduled only for applications in Submitted or Interviewing",
            stage=key,
        )
    demand = await ports.demand_row(db, app.demand_id)
    await plan.ensure_plan(db, demand)
    stage = await plan.stage_for_demand(db, demand.id, data.stage_id)
    await plan.run_schedule_guards(db, stage)  # Epic 40: no interviews in an assessment stage
    tz = data.timezone
    starts_at = timeutil.to_utc(data.start_local, tz)
    ends_at = starts_at + _duration(data.duration_minutes, stage.default_duration_minutes)
    at = now()
    send_invites = data.send_invites
    if starts_at < at:
        if not data.allow_past:
            raise ValidationFailed("The interview starts in the past", code="start_in_past")
        send_invites = False  # backfill: no invitations
    settings = await settings_row(db)
    tenant_info = await ports.tenant(db, tenant_of(db))
    candidate_email = await ports.candidate_email(db, app.candidate_id)
    if candidate_email is None and send_invites:
        raise conflict("candidate_email_missing", "The candidate has no email address")
    specs = await _resolve_participants(
        db,
        data.participants,
        interview_tz=tz,
        tenant_tz=tenant_info.timezone,
        candidate_email=candidate_email,
    )
    candidate_tz = _tz_or(data.candidate_timezone, tz)
    candidate = await ports.candidate_summary(db, app.candidate_id)
    previous = (
        await db.execute(
            select(func.count()).where(
                Interview.application_id == app.id,
                Interview.stage_id == stage.id,
                Interview.status != "cancelled",
            )
        )
    ).scalar_one()
    user_id = user_id_of(ctx)
    organizer = user_id or principal.id
    row = Interview(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, "INT", width=6),
        application_id=app.id,
        demand_id=app.demand_id,
        candidate_id=app.candidate_id,
        stage_id=stage.id,
        stage_name=stage.name,
        kit_snapshot=await _kit_snapshot(db, stage.kit_id, settings.rating_scale),
        round=int(previous) + 1,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=tz,
        mode=data.mode,
        meeting_url=data.meeting_url,
        location=data.location,
        notes=data.notes,
        status="scheduled",
        sequence=0,
        ical_uid=f"{uuid.uuid4()}@talent-os",
        organizer_user_id=organizer,
        provider="none",
        create_online_meeting=bool(data.create_online_meeting and data.mode == "video"),
        send_invites=send_invites,
        reminder_state=initial_reminder_state(starts_at, offsets(settings), at),
        version=1,
    )
    db.add(row)
    await db.flush()
    participants = [_participant(row, s) for s in specs]
    if candidate_email is not None:
        participants.append(
            _participant(
                row,
                {
                    "kind": "candidate",
                    "email": candidate_email,
                    "name": candidate.get("full_name"),
                    "timezone": candidate_tz,
                    "scorecard_required": False,
                },
            )
        )
    db.add_all(participants)
    await db.flush()

    if key == "submitted":
        await ports.transition(ctx, db, app.id, "interviewing", "system", note="First interview scheduled")
    await ports.bump_interviews_scheduled(ctx, db, app.demand_id, +1)
    await ports.first_interview_scheduled(ctx, db, app.demand_id)
    emit(
        db,
        InterviewScheduled(
            tenant_id=row.tenant_id,
            interview_id=row.id,
            application_id=row.application_id,
            demand_id=row.demand_id,
            candidate_id=row.candidate_id,
            stage_name=row.stage_name,
            starts_at=row.starts_at,
            interviewer_user_ids=tuple(
                p.user_id for p in participants if p.user_id and p.kind in INTERVIEWER_KINDS
            ),
            actor_user_id=user_id,
            send_invites=send_invites,
            origin_chain_depth=int(getattr(ctx, "workflow_chain_depth", 0) or 0),
        ),
    )
    await db.refresh(row)
    return await interview_out(db, row)


# ================================================================ reschedule / cancel / status
def _require_scheduled(row: Interview) -> None:
    if row.status != "scheduled":
        raise conflict(
            "interview_not_scheduled", "Only scheduled interviews can be changed", status=row.status
        )


async def _revoke_scorecard_links(db: AsyncSession, participant_ids: Iterable[uuid.UUID]) -> int:
    count = 0
    for pid in participant_ids:
        count += await secure_links.revoke_for_subject(db, PARTICIPANT_SUBJECT, pid, SCORECARD_PURPOSE)
    return count


async def reschedule_interview(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, data: InterviewReschedule
) -> InterviewOut:
    row = await interview_for(ctx, db, interview_id, manage=True, for_update=True)
    if data.expected_version != row.version:
        raise conflict(
            "version_conflict",
            "The interview changed in the meantime; reload and try again",
            version=row.version,
        )
    _require_scheduled(row)
    stage_duration = int((row.ends_at - row.starts_at).total_seconds() // 60)
    starts_at = timeutil.to_utc(data.start_local, data.timezone)
    ends_at = starts_at + _duration(data.duration_minutes, stage_duration)
    at = now()
    if starts_at < at:
        raise ValidationFailed("The interview starts in the past", code="start_in_past")
    settings = await settings_row(db)
    removed_ids: list[uuid.UUID] = []
    everyone = (await participants_of(db, [row.id], include_removed=True)).get(row.id, [])
    candidate_row = next((p for p in everyone if p.kind == "candidate"), None)
    if data.participants is not None:
        tenant_info = await ports.tenant(db, row.tenant_id)
        specs = await _resolve_participants(
            db,
            data.participants,
            interview_tz=data.timezone,
            tenant_tz=tenant_info.timezone,
            candidate_email=candidate_row.email if candidate_row else None,
        )
        by_email = {p.email.lower(): p for p in everyone if p.kind != "candidate"}
        wanted = {s["email"]: s for s in specs}
        for email_addr, p in by_email.items():
            if email_addr not in wanted and p.removed_at is None:
                p.removed_at = at
                removed_ids.append(p.id)
        for email_addr, spec in wanted.items():
            existing = by_email.get(email_addr)
            if existing is None:
                db.add(_participant(row, spec))
            else:
                existing.removed_at = None
                existing.kind = spec["kind"]
                existing.user_id = spec.get("user_id")
                existing.name = spec.get("name") or existing.name
                existing.timezone = spec["timezone"]
                existing.scorecard_required = bool(spec["scorecard_required"])
        await _revoke_scorecard_links(db, removed_ids)
    if data.candidate_timezone and candidate_row is not None:
        candidate_row.timezone = _tz_or(data.candidate_timezone, row.timezone)
    row.starts_at = starts_at
    row.ends_at = ends_at
    row.timezone = data.timezone
    if data.mode is not None:
        row.mode = data.mode
    if "meeting_url" in data.model_fields_set:
        row.meeting_url = data.meeting_url
    if "location" in data.model_fields_set:
        row.location = data.location
    if "notes" in data.model_fields_set:
        row.notes = data.notes
    row.sequence = row.sequence + 1
    row.reminder_state = initial_reminder_state(starts_at, offsets(settings), at)
    row.version = row.version + 1
    row.send_invites = True
    await db.flush()
    emit(
        db,
        InterviewRescheduled(
            tenant_id=row.tenant_id,
            interview_id=row.id,
            application_id=row.application_id,
            sequence=row.sequence,
            starts_at=row.starts_at,
            removed_participant_ids=tuple(removed_ids),
            actor_user_id=user_id_of(ctx),
        ),
    )
    await db.refresh(row)
    return await interview_out(db, row)


async def cancel_row(
    ctx: RequestContext, db: AsyncSession, row: Interview, reason: str, *, actor_kind: str = "user"
) -> None:
    """Cancel a scheduled interview (row already locked): CANCEL invites, scorecard links revoked."""
    _require_scheduled(row)
    row.status = "cancelled"
    row.cancelled_reason = reason
    row.sequence = row.sequence + 1
    row.version = row.version + 1
    await db.flush()
    parts = (await participants_of(db, [row.id], include_removed=True)).get(row.id, [])
    await _revoke_scorecard_links(db, [p.id for p in parts])
    await ports.bump_interviews_scheduled(ctx, db, row.demand_id, -1)
    emit(
        db,
        InterviewCancelled(
            tenant_id=row.tenant_id,
            interview_id=row.id,
            application_id=row.application_id,
            sequence=row.sequence,
            actor_user_id=user_id_of(ctx),
            actor_kind=actor_kind,
            send_invites=row.send_invites,
        ),
    )


async def cancel_interview(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, data: CancelIn
) -> InterviewOut:
    row = await interview_for(ctx, db, interview_id, manage=True, for_update=True)
    await cancel_row(ctx, db, row, data.reason)
    await db.refresh(row)
    return await interview_out(db, row)


async def cancel_open_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, reason: str
) -> int:
    rows = list(
        (
            await db.execute(
                select(Interview)
                .where(
                    Interview.application_id == application_id,
                    Interview.status == "scheduled",
                    Interview.starts_at > now(),  # interviews already under way are left alone
                )
                .with_for_update()
            )
        ).scalars()
    )
    for row in rows:
        await cancel_row(ctx, db, row, reason, actor_kind="system")
    return len(rows)


def _completed_event(row: Interview, status: str, actor_kind: str) -> InterviewCompleted:
    return InterviewCompleted(
        tenant_id=row.tenant_id,
        interview_id=row.id,
        application_id=row.application_id,
        demand_id=row.demand_id,
        stage_name=row.stage_name,
        status=status,
        actor_kind=actor_kind,
    )


async def mark_no_show(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> InterviewOut:
    row = await interview_for(ctx, db, interview_id, manage=True, for_update=True)
    if row.status not in ("scheduled", "completed"):
        raise conflict(
            "interview_not_scheduled",
            "Only scheduled or completed interviews can be marked no-show",
            status=row.status,
        )
    if row.starts_at > now():
        raise conflict("interview_not_started", "The interview has not started yet")
    row.status = "no_show"
    row.completed_at = row.completed_at or now()
    row.version = row.version + 1
    await db.flush()
    emit(db, _completed_event(row, "no_show", "user"))
    await db.refresh(row)
    return await interview_out(db, row)


async def complete_interview(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> InterviewOut:
    row = await interview_for(ctx, db, interview_id, manage=True, for_update=True)
    _require_scheduled(row)
    if row.starts_at > now():
        raise conflict("interview_not_started", "The interview has not started yet")
    row.status = "completed"
    row.completed_at = now()
    row.version = row.version + 1
    await db.flush()
    emit(db, _completed_event(row, "completed", "user"))
    await db.refresh(row)
    return await interview_out(db, row)


async def complete_elapsed(db: AsyncSession) -> int:
    """Beat: ``scheduled`` interviews whose ``ends_at`` passed -> ``completed`` (actor ``system``)."""
    at = now()
    rows = list(
        (
            await db.execute(
                select(Interview)
                .where(Interview.status == "scheduled", Interview.ends_at < at)
                .order_by(Interview.ends_at)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    with acting_as(db, "system"):
        for row in rows:
            row.status = "completed"
            row.completed_at = at
            row.version = row.version + 1
            emit(db, _completed_event(row, "completed", "system"))
        await db.flush()
    return len(rows)


# ================================================================ reads
async def get_interview(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> InterviewOut:
    return await interview_out(db, await interview_for(ctx, db, interview_id))


def _filters(stmt: Any, date_from: datetime | None, date_to: datetime | None, status: str | None) -> Any:
    if date_from is not None:
        stmt = stmt.where(Interview.ends_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Interview.starts_at < date_to)
    if status:
        stmt = stmt.where(Interview.status.in_([s for s in status.split(",") if s]))
    return stmt


async def _page(db: AsyncSession, stmt: Any, params: PageParams) -> Page[InterviewOut]:
    total = int(
        (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    )
    rows = list((await db.execute(stmt.limit(params.page_size).offset(params.offset))).scalars())
    return Page[InterviewOut](
        items=await interview_outs(db, rows), total=total, page=params.page, page_size=params.page_size
    )


async def list_for_demand(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    params: PageParams,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
) -> Page[InterviewOut]:
    principal = principal_of(ctx)
    demand = await ports.demand_visible(ctx, db, demand_id)
    check(principal, READ)
    stmt = _filters(select(Interview).where(Interview.demand_id == demand.id), date_from, date_to, status)
    return await _page(db, stmt.order_by(Interview.starts_at, Interview.id), params)


async def list_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[InterviewOut]:
    principal = principal_of(ctx)
    app = await ports.application_for(ctx, db, application_id)
    check(principal, READ)
    rows = (
        await db.execute(
            select(Interview)
            .where(Interview.application_id == app.id)
            .order_by(Interview.starts_at, Interview.id)
        )
    ).scalars()
    return await interview_outs(db, list(rows))


async def list_mine(
    ctx: RequestContext,
    db: AsyncSession,
    params: PageParams,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
) -> Page[InterviewOut]:
    principal = principal_of(ctx)
    mine = select(InterviewParticipant.interview_id).where(
        InterviewParticipant.user_id == principal.id, InterviewParticipant.removed_at.is_(None)
    )
    stmt = select(Interview).where(or_(Interview.organizer_user_id == principal.id, Interview.id.in_(mine)))
    stmt = _filters(stmt, date_from, date_to, status)
    return await _page(db, stmt.order_by(Interview.starts_at, Interview.id), params)


async def ics_for(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, participant_id: uuid.UUID | None
) -> bytes:
    """``GET /interviews/{id}/ics`` -- the invitation as the given participant (default: the caller) sees it."""
    from app.services.interviews import invites

    row = await interview_for(ctx, db, interview_id)
    parts = (await participants_of(db, [row.id])).get(row.id, [])
    target = None
    if participant_id is not None:
        target = next((p for p in parts if p.id == participant_id), None)
        if target is None:
            raise NotFound("Participant not found")
    else:
        uid = user_id_of(ctx)
        target = next((p for p in parts if uid and p.user_id == uid), None)
    tz = target.timezone if target is not None else row.timezone
    event = await invites.ics_event(
        db,
        row,
        parts,
        tz,
        cancelled=row.status == "cancelled",
        for_candidate=bool(target and target.kind == "candidate"),
    )
    from app.services.interviews import ics

    return ics.build(event)
