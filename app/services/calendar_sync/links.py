# ruff: file-ignore[line-too-long]
"""Event links and the version tag (Epic 72 stories 72.1 / 72.2, spec A3).

* ``make_tag(interview_id, sequence)`` -- ``tos1:<interview_id>:<sequence>:<nonce8>`` (registered as the
  ``connections_service`` tag provider, so P1 ``sync_invites`` writes it as a private extended property).
* ``on_event_written`` -- the ``connections_service`` event-written listener: upserts the organiser link with the
  tag / etag / times just written; a cancellation detaches it.
* ``attendee_link`` -- an internal participant's own copy, matched by iCalUID on their first delta sync.
"""

import secrets
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar_sync import CalendarEventLink
from app.services.calendar_sync.common import now


def make_tag(interview_id: uuid.UUID, sequence: int) -> str:
    return f"tos1:{interview_id}:{sequence}:{secrets.token_hex(4)}"


async def by_event(db: AsyncSession, connection_id: uuid.UUID, provider_event_id: str) -> CalendarEventLink | None:
    return (
        await db.execute(
            select(CalendarEventLink).where(
                CalendarEventLink.connection_id == connection_id,
                CalendarEventLink.provider_event_id == provider_event_id,
            )
        )
    ).scalar_one_or_none()


async def for_connection(db: AsyncSession, connection_id: uuid.UUID) -> list[CalendarEventLink]:
    return list(
        (
            await db.execute(
                select(CalendarEventLink).where(
                    CalendarEventLink.connection_id == connection_id, CalendarEventLink.state != "detached"
                )
            )
        ).scalars()
    )


async def for_interview(db: AsyncSession, interview_id: uuid.UUID) -> list[CalendarEventLink]:
    return list(
        (
            await db.execute(
                select(CalendarEventLink)
                .where(CalendarEventLink.interview_id == interview_id)
                .order_by(CalendarEventLink.created_at)
            )
        ).scalars()
    )


async def on_event_written(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    interview_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider: str,
    provider_event_id: str,
    ical_uid: str,
    sequence: int,
    tos_tag: str | None,
    etag: str | None,
    cancelled: bool,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    link = await by_event(db, connection_id, provider_event_id)
    if cancelled:
        if link is not None:
            link.state = "detached"
            await db.flush()
        return
    if tos_tag is None:
        return
    # a new organiser event for the Interview (re-created) detaches the previous ones
    for old in await for_interview(db, interview_id):
        if old.role == "organizer" and old.provider_event_id != provider_event_id and old.state != "detached":
            old.state = "detached"
    if link is None:
        link = CalendarEventLink(
            id=uuid.uuid4(), tenant_id=tenant_id, interview_id=interview_id, participant_id=None,
            connection_id=connection_id, provider=provider if provider in ("google", "microsoft", "fake") else "fake",
            provider_event_id=provider_event_id, ical_uid=ical_uid, role="organizer", tos_sequence=sequence,
            tos_tag=tos_tag, restore_count_window=[],
        )  # fmt: skip
        db.add(link)
    link.tos_sequence = sequence
    link.tos_tag = tos_tag
    link.tos_etag = etag
    link.seen_start, link.seen_end, link.seen_status = starts_at, ends_at, "confirmed"
    if link.state != "conflict":
        link.state = "in_sync"
    await db.flush()


async def attendee_link(
    db: AsyncSession,
    *,
    interview_id: uuid.UUID,
    participant_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider: str,
    provider_event_id: str,
    ical_uid: str,
    sequence: int,
) -> CalendarEventLink:
    from app.services.calendar_sync.common import tenant_of

    link = CalendarEventLink(
        id=uuid.uuid4(), tenant_id=tenant_of(db), interview_id=interview_id, participant_id=participant_id,
        connection_id=connection_id, provider=provider if provider in ("google", "microsoft", "fake") else "fake",
        provider_event_id=provider_event_id[:500], ical_uid=ical_uid[:200], role="attendee", tos_sequence=sequence,
        tos_tag=f"tos1:{interview_id}:{sequence}:attendee", restore_count_window=[], last_inbound_at=now(),
    )  # fmt: skip
    db.add(link)
    await db.flush()
    return link
