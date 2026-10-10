"""Invitations, reminders and scorecard nudges (7.2 / 7.3, AD-9). Called from Celery tasks only.

* ``sync_invites(db, interview_id, action)`` -- idempotent through ``interview_invite_log`` rows
  ``(interview_id, sequence, participant_id, method)``:
  - organiser connected (``connections_service.get_active``): ``provider.upsert_event`` / ``cancel_event``;
    the provider emails the attendees, so no .ics email goes to them (external interviewers still get their
    scorecard link by email). ``ProviderAuthError`` marks the connection ``needs_reconnect`` and falls back.
  - fallback: one email per participant with ``invite.ics`` (time rendered in the participant's zone, zone
    named). The candidate copy never shows notes or kit content; external interviewers get their scorecard
    secure link (re-issued per sequence: revoke + issue, expiry ``ends_at + external_scorecard_link_days_after``).
* ``send_due_reminders(db)`` -- per offset, claimed with ``FOR UPDATE SKIP LOCKED`` and recorded in
  ``reminder_state`` in the same transaction (exactly once per offset, even with concurrent workers).
* ``scorecard_reminders(db)`` -- one reminder per required participant without a submitted scorecard.
* Epic 59 (coordinator C-11, additive): ``register_reminder_owner(fn)``. While a registered owner (the Scheduling
  Agent) returns True for the tenant, both jobs skip a reminder that is less than ``OWNER_GRACE`` (10 minutes) overdue;
  later it is sent here as the fallback. With no owner (or the agent off) the behaviour is unchanged.

* Epic 72 (D-18, additive): ``_spec`` asks ``connections_service.event_tag`` for the version tag (None while no tag
  provider is registered = P1 behaviour); every successful provider write is reported through
  ``connections_service.notify_event_written``; ``resync_event`` re-writes the provider event with the current
  Interview times (calendar-sync restore / re-create) without sending any email.

Emails go through the organiser's connected mailbox when possible, else the platform mailer.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mailer, secure_links
from app.core.events import emit
from app.core.settings import get_settings
from app.events.interviews import ScorecardDue
from app.models.interviews import Interview, InterviewInviteLog, InterviewParticipant, Scorecard
from app.services.interviews import ports, timeutil
from app.services.interviews.common import acting_as, now, offsets, settings_row
from app.services.interviews.ics import IcsAttendee, IcsEvent, attachment
from app.services.interviews.scheduling import PARTICIPANT_SUBJECT, SCORECARD_PURPOSE, participants_of

MODE_LABELS = {"video": "Video call", "phone": "Phone", "in_person": "In person"}

# ================================================================ reminder owners (Epic 59, C-11)
ReminderOwner = Callable[[AsyncSession, uuid.UUID, str], Awaitable[bool]]
_REMINDER_OWNERS: list[ReminderOwner] = []
OWNER_GRACE = timedelta(minutes=10)


def register_reminder_owner(fn: ReminderOwner) -> None:
    """Register ``fn(db, tenant_id, kind)`` (kind ``reminder`` | ``scorecard``): True = another sender owns these
    reminders for the tenant now (P1 waits ``OWNER_GRACE`` before sending them as the fallback). Idempotent."""
    if fn not in _REMINDER_OWNERS:
        _REMINDER_OWNERS.append(fn)


async def _owned(db: AsyncSession, kind: str) -> bool:
    if not _REMINDER_OWNERS:
        return False
    tenant_id = uuid.UUID(str(db.sync_session.info.get("tenant_id")))
    for fn in list(_REMINDER_OWNERS):
        try:
            if await fn(db, tenant_id, kind):
                return True
        except Exception:  # an owner check never breaks the P1 job
            logger.exception("reminder owner check failed")
    return False


# ================================================================ content
async def _context(db: AsyncSession, row: Interview) -> dict[str, Any]:
    tenant = await ports.tenant(db, row.tenant_id)
    titles = await ports.demand_titles(db, [row.demand_id])
    candidate = await ports.candidate_summary(db, row.candidate_id)
    organizer = (await ports.users(db, [row.organizer_user_id])).get(row.organizer_user_id)
    return {
        "tenant_name": tenant.name,
        "demand_title": titles.get(row.demand_id) or "the role",
        "candidate_first": candidate.get("first_name") or "Candidate",
        "candidate_name": candidate.get("full_name") or "Candidate",
        "organizer_email": organizer.email if organizer else mailer_address(),
        "organizer_name": organizer.name if organizer else tenant.name,
    }


def mailer_address() -> str:
    from email.utils import parseaddr

    return parseaddr(get_settings().mail_from)[1] or "no-reply@talentos.local"


def scorecard_url(interview_id: uuid.UUID) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/interviews/{interview_id}/scorecard"


async def ics_event(
    db: AsyncSession,
    row: Interview,
    parts: Sequence[InterviewParticipant],
    recipient_tz: str,
    *,
    cancelled: bool,
    for_candidate: bool,
    ctx_data: dict[str, Any] | None = None,
) -> IcsEvent:
    data = ctx_data or await _context(db, row)
    summary = (
        f"{data['tenant_name']} interview for {data['demand_title']}"
        if for_candidate
        else f"Interview: {data['candidate_first']} – {data['demand_title']}"
    )
    lines = [
        f"{data['tenant_name']} – {row.stage_name} interview",
        f"When: {timeutil.render_for(row.starts_at, recipient_tz)}",
    ]
    if row.meeting_url:
        lines.append(f"Join: {row.meeting_url}")
    if row.location:
        lines.append(f"Location: {row.location}")
    return IcsEvent(
        uid=row.ical_uid,
        sequence=row.sequence,
        summary=summary,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        organizer_email=data["organizer_email"],
        organizer_name=data["organizer_name"],
        attendees=tuple(IcsAttendee(p.email, p.name, p.response_status) for p in parts),
        recipient_timezone=recipient_tz,
        description_lines=tuple(lines),
        location=row.location,
        url=row.meeting_url,
        cancelled=cancelled,
    )


def _when_lines(row: Interview, p: InterviewParticipant) -> list[str]:
    minutes = int((row.ends_at - row.starts_at).total_seconds() // 60)
    lines = [
        f"When: {timeutil.render_for(row.starts_at, p.timezone)} ({minutes} minutes)",
        f"Time zone: {p.timezone}",
        f"Format: {MODE_LABELS.get(row.mode, row.mode)}",
    ]
    if row.meeting_url:
        lines.append(f"Join: {row.meeting_url}")
    if row.location:
        lines.append(f"Location: {row.location}")
    return lines


def _subject(row: Interview, p: InterviewParticipant, data: dict[str, Any], method: str) -> str:
    if p.kind == "candidate":
        base = f"{data['tenant_name']} interview for {data['demand_title']}"
    else:
        base = f"Interview invitation: {data['candidate_first']} – {data['demand_title']}"
    if method == "CANCEL":
        return f"Cancelled: {base}"
    if row.sequence > 0:
        return f"Updated: {base}"
    return base


def _body(
    row: Interview, p: InterviewParticipant, data: dict[str, Any], method: str, link: str | None
) -> str:
    hello = f"Hello {p.name or 'there'},"
    if method == "CANCEL":
        intro = (
            f"The {row.stage_name} interview for {data['demand_title']} has been cancelled."
            if p.kind == "candidate"
            else f"The {row.stage_name} interview with {data['candidate_first']} for {data['demand_title']} has been cancelled."
        )
        return "\n".join(
            [
                hello,
                "",
                intro,
                "",
                f"It was planned for {timeutil.render_for(row.starts_at, p.timezone)}.",
                "",
                data["tenant_name"],
            ]
        )
    if p.kind == "candidate":
        intro = f"You are invited to a {row.stage_name} interview with {data['tenant_name']} for the {data['demand_title']} role."
        lines = [
            hello,
            "",
            intro,
            "",
            *_when_lines(row, p),
            "",
            "The calendar invitation is attached.",
            "",
            data["tenant_name"],
        ]
        return "\n".join(lines)
    intro = f"You are on the interview panel for {data['candidate_name']} ({data['demand_title']}), stage {row.stage_name}."
    lines = [hello, "", intro, "", *_when_lines(row, p)]
    if row.notes:
        lines += ["", "Notes from the organiser:", row.notes]
    if p.kind == "interviewer_external" and link:
        lines += ["", "After the interview, submit your scorecard here (single use):", link]
    elif p.kind == "interviewer_internal":
        lines += ["", f"Submit your scorecard in Talent OS after the interview: {scorecard_url(row.id)}"]
    lines += ["", data["tenant_name"]]
    return "\n".join(lines)


# ================================================================ scorecard links
async def issue_scorecard_link(
    db: AsyncSession, row: Interview, p: InterviewParticipant, *, expires_at: datetime | None = None
) -> str | None:
    """Revoke the participant's open scorecard link and issue a new one; returns the URL (None when expired)."""
    settings = await settings_row(db)
    expiry = expires_at or (row.ends_at + timedelta(days=settings.external_scorecard_link_days_after))
    ttl = expiry - datetime.now(UTC)  # secure links run on real time
    if ttl <= timedelta(0):
        return None
    await secure_links.revoke_for_subject(db, PARTICIPANT_SUBJECT, p.id, SCORECARD_PURPOSE)
    link, token = await secure_links.issue(
        db,
        tenant_id=row.tenant_id,
        purpose=SCORECARD_PURPOSE,
        subject_type=PARTICIPANT_SUBJECT,
        subject_id=p.id,
        ttl=ttl,
        max_uses=1,
        metadata={"interview_id": str(row.id)},
    )
    p.secure_link_id = link.id
    await db.flush()
    return secure_links.link_url(token)


async def current_link(db: AsyncSession, p: InterviewParticipant) -> secure_links.SecureLink | None:
    if p.secure_link_id is None:
        return None
    return await db.get(secure_links.SecureLink, p.secure_link_id)


# ================================================================ sending
async def _deliver(message: mailer.EmailMessage, conn: Any | None) -> str:
    """Organiser's mailbox (explicit connection, same session) else the platform mailer."""
    if conn is not None:
        try:
            await conn.impl.send_mail(conn.token_set, message)
            return "mailbox"
        except Exception as exc:
            logger.warning(
                "Mailbox send failed; platform mailer used (message={}, error={})",
                message.id,
                type(exc).__name__,
            )
    await mailer.send_email(message)
    return "platform"


async def _claim(
    db: AsyncSession, row: Interview, p: InterviewParticipant, method: str, channel: str
) -> uuid.UUID | None:
    """Claim the (interview, sequence, participant, method) slot; None when already sent / in progress."""
    stmt = (
        insert(InterviewInviteLog)
        .values(
            id=uuid.uuid4(),
            tenant_id=row.tenant_id,
            interview_id=row.id,
            participant_id=p.id,
            sequence=row.sequence,
            method=method,
            channel=channel,
        )
        .on_conflict_do_nothing(index_elements=["interview_id", "sequence", "participant_id", "method"])
        .returning(InterviewInviteLog.id)
    )
    claimed = (await db.execute(stmt)).scalar_one_or_none()
    if claimed is not None:
        return claimed
    existing = (
        await db.execute(
            select(InterviewInviteLog)
            .where(
                InterviewInviteLog.interview_id == row.id,
                InterviewInviteLog.sequence == row.sequence,
                InterviewInviteLog.participant_id == p.id,
                InterviewInviteLog.method == method,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.sent_at is None and existing.error is not None:
        existing.error = None
        existing.channel = channel
        await db.flush()
        return existing.id  # a failed earlier attempt: retry it
    return None


async def _mark(db: AsyncSession, log_id: uuid.UUID, error: str | None = None) -> None:
    log = await db.get(InterviewInviteLog, log_id)
    if log is not None:
        log.sent_at = None if error else now()
        log.error = error
        await db.flush()


def _email(
    row: Interview,
    p: InterviewParticipant,
    subject: str,
    text: str,
    kind: str,
    organizer: uuid.UUID,
    attachments: tuple[mailer.EmailAttachment, ...] = (),
) -> mailer.EmailMessage:
    return mailer.EmailMessage(
        to=p.email,
        subject=subject,
        text=text,
        tenant_id=row.tenant_id,
        kind=kind,
        attachments=attachments,
        sender_user_id=organizer,
        headers={"X-TOS-Interview-Id": str(row.id)},
    )


async def _send_ics(
    db: AsyncSession,
    row: Interview,
    p: InterviewParticipant,
    method: str,
    parts: Sequence[InterviewParticipant],
    data: dict[str, Any],
    conn: Any | None,
) -> bool:
    log_id = await _claim(db, row, p, method, "ics_email")
    if log_id is None:
        return False
    try:
        link = None
        if method == "REQUEST" and p.kind == "interviewer_external" and p.scorecard_required:
            link = await issue_scorecard_link(db, row, p)
        event = await ics_event(
            db,
            row,
            parts,
            p.timezone,
            cancelled=method == "CANCEL",
            for_candidate=p.kind == "candidate",
            ctx_data=data,
        )
        message = _email(
            row,
            p,
            _subject(row, p, data, method),
            _body(row, p, data, method, link),
            "interview_cancel" if method == "CANCEL" else "interview_invite",
            row.organizer_user_id,
            (attachment(event),),
        )
        await _deliver(message, conn)
    except Exception as exc:
        await _mark(db, log_id, type(exc).__name__[:200])
        raise
    await _mark(db, log_id)
    return True


async def _send_link_only(
    db: AsyncSession, row: Interview, p: InterviewParticipant, data: dict[str, Any], conn: Any | None
) -> bool:
    """Send via the provider path: the calendar invite comes from the provider; external interviewers still
    need their link."""
    log_id = await _claim(db, row, p, "REQUEST", "provider")
    if log_id is None:
        return False
    if p.kind == "interviewer_external" and p.scorecard_required:
        link = await issue_scorecard_link(db, row, p)
        if link:
            text = "\n".join(
                [
                    f"Hello {p.name or 'there'},",
                    "",
                    f"You will receive a calendar invitation for the {row.stage_name} interview with {data['candidate_first']} "
                    f"({data['demand_title']}) on {timeutil.render_for(row.starts_at, p.timezone)}.",
                    f"Time zone: {p.timezone}",
                    "",
                    "After the interview, submit your scorecard here (single use):",
                    link,
                    "",
                    data["tenant_name"],
                ]
            )
            await _deliver(
                _email(
                    row,
                    p,
                    f"Scorecard link: {data['candidate_first']} – {data['demand_title']}",
                    text,
                    "interview_scorecard_link",
                    row.organizer_user_id,
                ),
                conn,
            )
    await _mark(db, log_id)
    return True


def _spec(
    row: Interview,
    parts: Sequence[InterviewParticipant],
    data: dict[str, Any],
    *,
    sequence: int | None = None,
    recreate: bool = False,
) -> Any:
    from app.services import connections_service
    from app.services.connections.provider import Attendee, EventSpec

    seq = row.sequence if sequence is None else sequence

    description = "\n".join(
        [
            f"{data['tenant_name']} – {row.stage_name} interview for {data['demand_title']}",
            *([f"Location: {row.location}"] if row.location else []),
            *([f"Join: {row.meeting_url}"] if row.meeting_url else []),
        ]
    )
    return EventSpec(
        ical_uid=row.ical_uid,
        summary=f"Interview: {data['candidate_first']} – {data['demand_title']}",
        description=description,
        start_utc=row.starts_at,
        end_utc=row.ends_at,
        timezone=row.timezone,
        attendees=tuple(
            Attendee(p.email, p.name) for p in parts if p.email != (data.get("organizer_email") or "").lower()
        ),
        location=row.location,
        meeting_url=row.meeting_url,
        create_online_meeting=row.create_online_meeting,
        provider_event_id=None if recreate else row.provider_event_id,
        sequence=seq,
        tos_tag=connections_service.event_tag(row.id, seq),
    )


async def sync_invites(db: AsyncSession, interview_id: uuid.UUID, action: str = "request") -> dict[str, int]:
    """Send (or update / cancel) the invitations of one interview. Safe to run repeatedly."""
    from app.services import connections_service
    from app.services.connections.provider import ProviderAuthError, ProviderError

    row = (
        await db.execute(select(Interview).where(Interview.id == interview_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return {"sent": 0}
    if not row.send_invites:  # backfilled interviews never send invitations
        return {"sent": 0}
    everyone = (await participants_of(db, [row.id], include_removed=True)).get(row.id, [])
    active = [p for p in everyone if p.removed_at is None]
    removed = [p for p in everyone if p.removed_at is not None]
    cancel_all = row.status == "cancelled" or action == "cancel"
    if row.status not in ("scheduled", "cancelled"):
        return {"sent": 0}
    data = await _context(db, row)
    sent = 0
    with acting_as(db, "system"):
        conn = await connections_service.get_active(db, row.organizer_user_id)
        provider_ok = False
        if conn is not None:
            try:
                if cancel_all:
                    if row.provider_event_id:
                        await conn.impl.cancel_event(conn.token_set, row.provider_event_id)
                        await _written(db, row, conn, row.provider_event_id, None, None, cancelled=True)
                    provider_ok = bool(row.provider_event_id)
                else:
                    spec = _spec(row, active, data)
                    result = await conn.impl.upsert_event(conn.token_set, spec)
                    row.provider_event_id = result.provider_event_id
                    row.provider = conn.provider
                    row.provider_connection_id = conn.connection_id
                    if result.meeting_url:
                        row.meeting_url = result.meeting_url
                    await db.flush()
                    etag = getattr(result, "etag", None)
                    await _written(db, row, conn, result.provider_event_id, spec, etag)
                    provider_ok = True
            except ProviderAuthError:
                await connections_service.mark_needs_reconnect(
                    db, conn.connection_id, "calendar_unauthorized"
                )
                conn = None
            except ProviderError as exc:
                logger.warning(
                    "Calendar provider call failed; .ics fallback (interview={}, error={})",
                    row.id,
                    type(exc).__name__,
                )
                conn = None
        if cancel_all:
            for p in active:
                if provider_ok:
                    log_id = await _claim(db, row, p, "CANCEL", "provider")
                    if log_id is not None:
                        await _mark(db, log_id)
                        sent += 1
                elif await _send_ics(db, row, p, "CANCEL", active, data, conn):
                    sent += 1
            return {"sent": sent}
        for p in active:
            if provider_ok:
                if await _send_link_only(db, row, p, data, conn):
                    sent += 1
            elif await _send_ics(db, row, p, "REQUEST", active, data, conn):
                sent += 1
        for p in removed:
            methods = set(
                (
                    await db.execute(
                        select(InterviewInviteLog.method).where(InterviewInviteLog.participant_id == p.id)
                    )
                ).scalars()
            )
            if (
                "REQUEST" in methods
                and "CANCEL" not in methods
                and await _send_ics(db, row, p, "CANCEL", [p], data, None if provider_ok else conn)
            ):
                sent += 1
    return {"sent": sent}


async def _written(
    db: AsyncSession,
    row: Interview,
    conn: Any,
    provider_event_id: str,
    spec: Any,
    etag: str | None,
    *,
    cancelled: bool = False,
) -> None:
    """Epic 72: report a provider write to the registered listeners (calendar sync event links)."""
    from app.services import connections_service

    await connections_service.notify_event_written(
        db, tenant_id=row.tenant_id, interview_id=row.id, connection_id=conn.connection_id,
        provider=conn.provider, provider_event_id=provider_event_id, ical_uid=row.ical_uid,
        sequence=spec.sequence if spec is not None else row.sequence,
        tos_tag=spec.tos_tag if spec is not None else None, etag=etag, cancelled=cancelled,
        starts_at=row.starts_at, ends_at=row.ends_at,
    )  # fmt: skip


async def resync_event(
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    reason: str,
    sequence: int | None = None,
    recreate: bool = False,
) -> str | None:
    """Write the organiser's provider event again with the current Interview times (Epic 72, additive).

    Used to restore after an external move or re-create after a deletion. Sends no email. Returns the provider
    event id, or None when the Interview is not scheduled / the organiser has no usable connection."""
    from app.services import connections_service
    from app.services.connections.provider import ProviderAuthError, ProviderError

    row = (
        await db.execute(select(Interview).where(Interview.id == interview_id).with_for_update())
    ).scalar_one_or_none()
    if row is None or row.status != "scheduled":
        return None
    active = (await participants_of(db, [row.id])).get(row.id, [])
    with acting_as(db, "system"):
        conn = await connections_service.get_active(db, row.organizer_user_id)
        if conn is None:
            return None
        data = await _context(db, row)
        spec = _spec(row, active, data, sequence=sequence, recreate=recreate)
        try:
            result = await conn.impl.upsert_event(conn.token_set, spec)
        except ProviderAuthError:
            await connections_service.mark_needs_reconnect(db, conn.connection_id, "calendar_unauthorized")
            return None
        except ProviderError as exc:
            logger.warning(
                "Calendar resync failed (interview={}, reason={}, error={})", row.id, reason, type(exc).__name__
            )
            return None
        row.provider_event_id = result.provider_event_id
        row.provider = conn.provider
        row.provider_connection_id = conn.connection_id
        await db.flush()
        await _written(db, row, conn, result.provider_event_id, spec, getattr(result, "etag", None))
    return result.provider_event_id


# ================================================================ reminders
async def send_due_reminders(db: AsyncSession) -> int:
    """Send each configured reminder exactly once per interview and offset (claim + mark in one transaction)."""
    settings = await settings_row(db)
    offset_list = offsets(settings)
    if not offset_list:
        return 0
    at = now()
    horizon = at + timedelta(minutes=max(offset_list))
    rows = list(
        (
            await db.execute(
                select(Interview)
                .where(
                    Interview.status == "scheduled",
                    Interview.send_invites.is_(True),
                    Interview.starts_at > at,
                    Interview.starts_at <= horizon,
                )
                .order_by(Interview.starts_at)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    count = 0
    owned = await _owned(db, "reminder")
    with acting_as(db, "system"):
        for row in rows:
            state = dict(row.reminder_state or {})
            due = [
                o for o in offset_list if not state.get(str(o)) and at >= row.starts_at - timedelta(minutes=o)
            ]
            if not due:
                continue
            send_offset = min(due)  # the most imminent; older ones that were missed are skipped
            if owned and at - (row.starts_at - timedelta(minutes=send_offset)) < OWNER_GRACE:
                continue  # Epic 59: the Scheduling Agent sends it; P1 is the fallback once it is overdue
            for o in due:
                state[str(o)] = "skipped"
            parts = (await participants_of(db, [row.id])).get(row.id, [])
            recipients = [
                p for p in parts if p.kind in ("candidate", "interviewer_internal", "interviewer_external")
            ]
            data = await _context(db, row)
            for p in recipients:
                await mailer.send_routed(_reminder(row, p, data))
                count += 1
            state[str(send_offset)] = at.isoformat()
            row.reminder_state = state
        await db.flush()
    return count


def _reminder(row: Interview, p: InterviewParticipant, data: dict[str, Any]) -> mailer.EmailMessage:
    base = (
        f"{data['tenant_name']} interview for {data['demand_title']}"
        if p.kind == "candidate"
        else f"Interview: {data['candidate_first']} – {data['demand_title']}"
    )
    lines = [
        f"Hello {p.name or 'there'},",
        "",
        f"Reminder: your {row.stage_name} interview is coming up.",
        "",
        *_when_lines(row, p),
        "",
        data["tenant_name"],
    ]
    return _email(row, p, f"Reminder: {base}", "\n".join(lines), "interview_reminder", row.organizer_user_id)


async def scorecard_reminders(db: AsyncSession) -> int:
    """One reminder per required participant whose scorecard is still missing ``after_hours`` after the end."""
    settings = await settings_row(db)
    at = now()
    cutoff = at - timedelta(hours=settings.scorecard_reminder_after_hours)
    submitted = select(Scorecard.participant_id).where(Scorecard.status == "submitted")
    rows = (
        await db.execute(
            select(InterviewParticipant, Interview)
            .join(Interview, Interview.id == InterviewParticipant.interview_id)
            .where(
                Interview.status == "completed",
                Interview.ends_at <= cutoff,
                InterviewParticipant.scorecard_required.is_(True),
                InterviewParticipant.removed_at.is_(None),
                InterviewParticipant.reminded_at.is_(None),
                InterviewParticipant.id.not_in(submitted),
            )
            .limit(500)
            .with_for_update(of=InterviewParticipant, skip_locked=True)
        )
    ).all()
    count = 0
    owned = await _owned(db, "scorecard")
    grace_cutoff = cutoff - OWNER_GRACE
    with acting_as(db, "system"):
        for p, row in rows:
            if owned and row.ends_at > grace_cutoff:
                continue  # Epic 59: the Scheduling Agent nudges; P1 is the fallback once it is overdue
            data = await _context(db, row)
            p.reminded_at = at
            subject = f"Scorecard due: {data['candidate_first']} – {data['demand_title']}"
            intro = [
                f"Hello {p.name or 'there'},",
                "",
                f"Your scorecard for the {row.stage_name} interview on {timeutil.render_for(row.starts_at, p.timezone)} is still missing.",
            ]
            if p.kind == "interviewer_internal" and p.user_id is not None:
                text = "\n".join(
                    [*intro, "", f"Submit it in Talent OS: {scorecard_url(row.id)}", "", data["tenant_name"]]
                )
                await mailer.send_routed(
                    _email(row, p, subject, text, "scorecard_reminder", row.organizer_user_id)
                )
                emit(
                    db,
                    ScorecardDue(
                        tenant_id=row.tenant_id,
                        interview_id=row.id,
                        application_id=row.application_id,
                        interviewer_user_id=p.user_id,
                    ),
                )
                count += 1
            elif p.kind == "interviewer_external":
                link = await current_link(db, p)
                if (
                    link is None
                    or link.revoked_at is not None
                    or link.use_count >= link.max_uses
                    or link.expires_at <= at
                ):
                    continue  # expired (or never issued): no reminder
                # Only link hashes are stored: the same link is re-issued with the same expiry.
                url = await issue_scorecard_link(db, row, p, expires_at=link.expires_at)
                if url is None:
                    continue
                text = "\n".join([*intro, "", "Submit it here (single use):", url, "", data["tenant_name"]])
                await mailer.send_routed(
                    _email(row, p, subject, text, "scorecard_reminder", row.organizer_user_id)
                )
                count += 1
        await db.flush()
    return count
