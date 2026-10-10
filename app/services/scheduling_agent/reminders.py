# ruff: noqa: E501, D401
"""Reminders and scorecard nudges (Epic 59 story 59.5, FR-300; spec A5).

* ``InterviewScheduled`` / ``InterviewRescheduled`` -> one ``before_interview`` row per participant (candidate, internal
  and external interviewers) x ``reminder_offsets_minutes`` for the interview's current ``sequence``; rows of older
  sequences are ``superseded``. ``InterviewCancelled`` -> ``superseded``.
* ``InterviewCompleted(status=completed)`` -> ``scorecard_nudge`` rows (``ends_at + n x scorecard_nudge_after_hours``,
  n = 1..``scorecard_nudge_max``) for required interviewers without a submitted scorecard; ``ScorecardSubmitted`` ->
  ``superseded``.
* ``dispatch`` (beat, every 5 minutes): when the agent is on and the trigger enabled, due rows are grouped by the
  interview organiser (<= 25 per run) and handed to a reminder run (``trigger_key`` ``reminders`` / ``scorecard_nudges``,
  no subject, ``owner = organiser``).
* ``send`` (tool ``reminders.send``): candidate -> the platform's approved reminder email (+ the tenant's WhatsApp template
  when configured and opted in), in the Candidate's zone; internal interviewer -> in-app + email notification; external
  interviewer -> email (nudges carry the P1 single-use scorecard link). The P1 ``reminder_state`` (or the participant's
  ``reminded_at``) is marked ``agent`` so P1 never duplicates.
* P1 suppression (C-11): ``owns_reminders`` is registered with ``interviews.invites.register_reminder_owner``; P1 skips a
  reminder while it returns True and the reminder is less than 10 minutes overdue (the P1 jobs stay the fallback).
"""

import json
import uuid
from datetime import datetime, timedelta
from html import escape
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.scheduling_agent import SchedulingReminder
from app.services import entity_refs
from app.services.scheduling_agent.common import (
    AGENT_KEY,
    agent_active,
    now,
    settings_row,
    system_ctx,
    tenant_of,
)

BATCH = 8  # a run makes at most 10 writes (RunLimits ceiling); spec "<= 25" narrowed
REMINDER_PARTICIPANTS = ("candidate", "interviewer_internal", "interviewer_external")
INTERVIEWER_KINDS = ("interviewer_internal", "interviewer_external")


# ================================================================ rows
async def on_scheduled(db: AsyncSession, interview_id: uuid.UUID) -> int:
    from app.models.interviews import Interview, InterviewParticipant

    row = await db.get(Interview, interview_id)
    if row is None or row.status != "scheduled" or not row.send_invites:
        return 0
    await db.execute(
        update(SchedulingReminder)
        .where(
            SchedulingReminder.interview_id == row.id,
            SchedulingReminder.kind == "before_interview",
            SchedulingReminder.sequence != row.sequence,
            SchedulingReminder.status == "due",
        )
        .values(status="superseded")
    )
    settings = await settings_row(db)
    parts = (
        (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == row.id,
                    InterviewParticipant.removed_at.is_(None),
                    InterviewParticipant.kind.in_(REMINDER_PARTICIPANTS),
                )
            )
        )
        .scalars()
        .all()
    )
    at = now()
    count = 0
    for offset in sorted(
        {int(o) for o in settings.reminder_offsets_minutes or [] if int(o) > 0}, reverse=True
    ):
        due = row.starts_at - timedelta(minutes=offset)
        if due <= at:
            continue
        for p in parts:
            result = await db.execute(
                pg_insert(SchedulingReminder)
                .values(
                    id=uuid.uuid4(), tenant_id=row.tenant_id, interview_id=row.id, participant_id=p.id,
                    candidate_id=row.candidate_id if p.kind == "candidate" else None, kind="before_interview",
                    offset_minutes=offset, sequence=row.sequence, due_at=due, status="due",
                )
                .on_conflict_do_nothing(constraint="uq_scheduling_reminders_natural")
            )  # fmt: skip
            count += int(getattr(result, "rowcount", 0) or 0)
    await db.flush()
    return count


async def on_cancelled(db: AsyncSession, interview_id: uuid.UUID) -> None:
    await db.execute(
        update(SchedulingReminder)
        .where(SchedulingReminder.interview_id == interview_id, SchedulingReminder.status == "due")
        .values(status="superseded")
    )


async def on_completed(db: AsyncSession, interview_id: uuid.UUID, status: str) -> int:
    from app.models.interviews import Interview, InterviewParticipant, Scorecard

    await db.execute(
        update(SchedulingReminder)
        .where(
            SchedulingReminder.interview_id == interview_id,
            SchedulingReminder.kind == "before_interview",
            SchedulingReminder.status == "due",
        )
        .values(status="superseded")
    )
    if status != "completed":
        return 0
    row = await db.get(Interview, interview_id)
    if row is None:
        return 0
    settings = await settings_row(db)
    submitted = select(Scorecard.participant_id).where(Scorecard.status == "submitted")
    parts = (
        (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id == row.id,
                    InterviewParticipant.removed_at.is_(None),
                    InterviewParticipant.scorecard_required.is_(True),
                    InterviewParticipant.kind.in_(INTERVIEWER_KINDS),
                    InterviewParticipant.id.not_in(submitted),
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for n in range(1, int(settings.scorecard_nudge_max) + 1):
        minutes = int(settings.scorecard_nudge_after_hours) * 60 * n
        for p in parts:
            result = await db.execute(
                pg_insert(SchedulingReminder)
                .values(
                    id=uuid.uuid4(), tenant_id=row.tenant_id, interview_id=row.id, participant_id=p.id, candidate_id=None,
                    kind="scorecard_nudge", offset_minutes=minutes, sequence=row.sequence,
                    due_at=row.ends_at + timedelta(minutes=minutes), status="due",
                )
                .on_conflict_do_nothing(constraint="uq_scheduling_reminders_natural")
            )  # fmt: skip
            count += int(getattr(result, "rowcount", 0) or 0)
    await db.flush()
    return count


async def on_scorecard_submitted(
    db: AsyncSession, interview_id: uuid.UUID, participant_id: uuid.UUID
) -> None:
    await db.execute(
        update(SchedulingReminder)
        .where(
            SchedulingReminder.interview_id == interview_id,
            SchedulingReminder.participant_id == participant_id,
            SchedulingReminder.kind == "scorecard_nudge",
            SchedulingReminder.status == "due",
        )
        .values(status="superseded")
    )


# ================================================================ P1 suppression hook (C-11)
async def owns_reminders(db: AsyncSession, tenant_id: uuid.UUID, kind: str) -> bool:  # noqa: ARG001
    """True while the Scheduling Agent sends this tenant's reminders (``reminder``) / nudges (``scorecard``)."""
    try:
        return await agent_active(db, "reminders" if kind == "reminder" else "scorecard_nudges")
    except Exception:
        logger.exception("scheduling reminder owner check failed")
        return False


# ================================================================ dispatch (beat)
async def dispatch(db: AsyncSession) -> dict[str, int]:
    """Start reminder / nudge runs for due rows (<= 25 per organiser per tick)."""
    from app.models.interviews import Interview

    out = {"reminder_runs": 0, "nudge_runs": 0, "skipped": 0}
    at = now()
    # rows whose moment passed (interview started / already scorecarded) are skipped
    stale = await db.execute(
        update(SchedulingReminder)
        .where(
            SchedulingReminder.status == "due",
            SchedulingReminder.kind == "before_interview",
            SchedulingReminder.due_at < at - timedelta(hours=6),
        )
        .values(status="skipped")
    )
    out["skipped"] = int(getattr(stale, "rowcount", 0) or 0)
    for kind, trigger, key in (
        ("before_interview", "reminders", "reminder_runs"),
        ("scorecard_nudge", "scorecard_nudges", "nudge_runs"),
    ):
        if not await agent_active(db, trigger):
            continue
        rows = (
            await db.execute(
                select(SchedulingReminder, Interview.organizer_user_id)
                .join(Interview, Interview.id == SchedulingReminder.interview_id)
                .where(
                    SchedulingReminder.status == "due",
                    SchedulingReminder.kind == kind,
                    SchedulingReminder.due_at <= at,
                    SchedulingReminder.run_id.is_(None),
                )
                .order_by(SchedulingReminder.due_at)
                .limit(500)
                .with_for_update(of=SchedulingReminder, skip_locked=True)
            )
        ).all()
        groups: dict[uuid.UUID, list[SchedulingReminder]] = {}
        for rem, organiser in rows:
            groups.setdefault(organiser, []).append(rem)
        for organiser, items in groups.items():
            for i in range(0, len(items), BATCH):
                batch = items[i : i + BATCH]
                run_id = await _start(
                    db,
                    organiser,
                    trigger,
                    [r.id for r in batch],
                    at,
                    [r.candidate_id for r in batch if r.candidate_id],
                )
                if run_id is not None:
                    for r in batch:
                        r.run_id = run_id
                    out[key] += 1
    await db.flush()
    return out


async def _start(
    db: AsyncSession,
    organiser: uuid.UUID,
    trigger: str,
    ids: list[uuid.UUID],
    at: datetime,
    candidate_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID | None:
    from app.core.agentic import Kickoff
    from app.services import agent_runtime_service as runtime

    tick = at.strftime("%Y%m%d%H%M")
    kick = Kickoff(
        inputs={
            "mode": "reminders" if trigger == "reminders" else "nudges",
            "reminder_ids": json.dumps([str(i) for i in ids]),
        },
        candidate_ids=tuple(dict.fromkeys(candidate_ids or [])),  # the send policy's known recipients
    )
    ref = await runtime.start_run(
        system_ctx(tenant_of(db)), db, agent_key=AGENT_KEY, trigger_kind="schedule", trigger_key=trigger,
        subject=None, kickoff=kick, owner_user_id=organiser, dedup_key=f"sched:rem:{organiser}:{trigger}:{tick}:{ids[0]}",
    )  # fmt: skip
    return ref.run_id if ref.reason not in ("attached", "deduplicated") else None


# ================================================================ send (tool)
def _when(starts_at: datetime, tz: str) -> str:
    from app.services.interviews import timeutil

    return timeutil.render_for(starts_at, tz)


async def _mark_p1(db: AsyncSession, interview: Any, at: datetime) -> None:
    """P1 ``reminder_state``: every P1 offset already due is marked ``agent`` (P1 never sends it again)."""
    from app.services.interviews.common import offsets as p1_offsets
    from app.services.interviews.common import settings_row as p1_settings

    offsets = p1_offsets(await p1_settings(db))
    state = dict(interview.reminder_state or {})
    for o in offsets:
        if at >= interview.starts_at - timedelta(minutes=o) and not state.get(str(o)):
            state[str(o)] = "agent"
    interview.reminder_state = state


async def send(
    ctx: RequestContext, db: AsyncSession, reminder_id: uuid.UUID, run_id: uuid.UUID | None
) -> dict[str, Any]:
    """Send one reminder / nudge. Returns ``{"status", "channel"}``; a row outside the run's batch is ``not_found``."""
    from app.models.interviews import Interview, InterviewParticipant

    rem = (
        await db.execute(
            select(SchedulingReminder).where(SchedulingReminder.id == reminder_id).with_for_update()
        )
    ).scalar_one_or_none()
    if rem is None or (run_id is not None and rem.run_id != run_id):
        raise NotFound("Reminder not found")
    if rem.status != "due":
        return {"status": rem.status, "channel": rem.channel}
    interview = await db.get(Interview, rem.interview_id)
    p = await db.get(InterviewParticipant, rem.participant_id)
    at = now()
    expected = "scheduled" if rem.kind == "before_interview" else "completed"
    if (
        interview is None
        or p is None
        or p.removed_at is not None
        or interview.status != expected
        or (interview.sequence != rem.sequence and rem.kind == "before_interview")
    ):
        rem.status = "skipped"
        await db.flush()
        return {"status": "skipped", "channel": None}
    tenant = await entity_refs.tenant(db, interview.tenant_id)
    channel: str | None = None
    message_ref: uuid.UUID | None = None
    if rem.kind == "before_interview":
        if p.kind == "candidate":
            channel, message_ref = await _candidate_reminder(ctx, db, interview, p, tenant.name)
        elif p.kind == "interviewer_internal" and p.user_id is not None:
            channel = await _notify(db, "scheduling_interview_reminder", interview, p)
        else:
            channel = await _external_mail(interview, p, tenant.name, nudge=False, db=db)
        await _mark_p1(db, interview, at)
    else:
        if p.kind == "interviewer_internal" and p.user_id is not None:
            channel = await _notify(db, "scheduling_scorecard_nudge", interview, p)
        else:
            channel = await _external_mail(interview, p, tenant.name, nudge=True, db=db)
        p.reminded_at = at  # P1 scorecard_reminders never repeats it
    rem.status = "sent" if channel else "failed"
    rem.channel, rem.message_ref, rem.sent_at = channel, message_ref, at if channel else None
    await db.flush()
    return {"status": rem.status, "channel": channel}


async def _candidate_reminder(
    ctx: RequestContext, db: AsyncSession, interview: Any, p: Any, tenant_name: str
) -> tuple[str | None, uuid.UUID | None]:
    from app.models.demands import Demand
    from app.services import comms_service

    settings = await settings_row(db)
    channels = list(settings.reminder_channels or ["email"])
    demand = await db.get(Demand, interview.demand_id)
    title = demand.title if demand is not None else "the role"
    sent: str | None = None
    message_id: uuid.UUID | None = None
    if "email" in channels:
        try:
            recipient = await comms_service.recipient_for(db, "candidate", interview.candidate_id)
            body = (
                f"<p>Hello {escape(p.name or 'there')},</p>"
                f"<p>Reminder: your {escape(interview.stage_name)} interview for {escape(title)} with {escape(tenant_name)} is on "
                f"{escape(_when(interview.starts_at, p.timezone))}.</p>"
                + (
                    f'<p>Join: <a href="{escape(interview.meeting_url)}">{escape(interview.meeting_url)}</a></p>'
                    if interview.meeting_url
                    else ""
                )
                + "<p>Need a different time? Reply to this email or use the candidate portal.</p>"
            )
            result = await comms_service.queue_email(
                ctx, db, origin="agent" if getattr(ctx, "actor_kind", None) == "agent" else "system", to=[recipient],
                subject=f"Reminder: {interview.stage_name} interview - {title}", body_html=body,
                related=[("candidate", interview.candidate_id), ("application", interview.application_id)],
                refs={"candidate_id": interview.candidate_id, "demand_id": interview.demand_id},
                reply_to_user_id=interview.organizer_user_id, raw=True,
            )  # fmt: skip
            message_id = result.message_ids[0] if result.message_ids else None
            sent = "email" if message_id else None
        except Exception:
            logger.info("Scheduling reminder: no email for interview {}", interview.id)
    if "whatsapp" in channels and settings.whatsapp_template_id is not None:
        from app.schemas.messaging import RecipientRef
        from app.services.messaging import sending

        try:
            async with db.begin_nested():
                result = await sending.send(
                    ctx, db, "whatsapp", settings.whatsapp_template_id,
                    [RecipientRef(entity_type="candidate", entity_id=interview.candidate_id)],
                    {"candidate_id": str(interview.candidate_id), "demand_id": str(interview.demand_id)}, origin="agent",
                )  # fmt: skip
            first = result.results[0] if result.results else None
            if first is not None and first.message_id is not None:
                sent = sent or "whatsapp"
                message_id = message_id or first.message_id
        except Exception:
            logger.info("Scheduling reminder: WhatsApp not sent for interview {}", interview.id)
    return sent, message_id


async def _notify(db: AsyncSession, type_key: str, interview: Any, p: Any) -> str | None:
    from app.services import notifications_service as ns

    try:
        n = await ns.fan_out(db, type_key, {
            "user_id": str(p.user_id), "interview_id": str(interview.id), "application_id": str(interview.application_id),
            "event_id": f"{type_key}:{interview.id}:{p.id}:{interview.sequence}:{now().strftime('%Y%m%d%H')}",
        })  # fmt: skip
    except Exception:
        logger.exception("Scheduling notification {} failed", type_key)
        return None
    return "in_app" if n else None


async def _external_mail(
    interview: Any, p: Any, tenant_name: str, *, nudge: bool, db: AsyncSession
) -> str | None:
    from app.core import mailer
    from app.services.interviews import invites

    if nudge:
        link = await invites.current_link(db, p)
        if link is None or link.revoked_at is not None or link.expires_at <= now():
            return None
        url = await invites.issue_scorecard_link(db, interview, p, expires_at=link.expires_at)
        if url is None:
            return None
        subject = f"Scorecard due: {interview.stage_name} interview"
        text = f"Hello {p.name or 'there'},\n\nYour scorecard for the {interview.stage_name} interview on {_when(interview.starts_at, p.timezone)} is still missing.\n\nSubmit it here (single use):\n{url}\n\n{tenant_name}"
    else:
        subject = f"Reminder: {interview.stage_name} interview"
        text = f"Hello {p.name or 'there'},\n\nReminder: the {interview.stage_name} interview is on {_when(interview.starts_at, p.timezone)}.\n\n{tenant_name}"
    try:
        await mailer.send_routed(
            mailer.EmailMessage(
                to=p.email, subject=subject, text=text, tenant_id=interview.tenant_id,
                kind="scorecard_reminder" if nudge else "interview_reminder", sender_user_id=interview.organizer_user_id,
                headers={"X-TOS-Interview-Id": str(interview.id)},
            )
        )  # fmt: skip
    except Exception:
        logger.exception("Scheduling reminder email failed")
        return None
    return "email"


async def batch_of(db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(select(SchedulingReminder.id).where(SchedulingReminder.run_id == run_id))
    return [r[0] for r in rows.all()]


__all__ = [
    "batch_of",
    "dispatch",
    "on_cancelled",
    "on_completed",
    "on_scheduled",
    "on_scorecard_submitted",
    "owns_reminders",
    "send",
]
