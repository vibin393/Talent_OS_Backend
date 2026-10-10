# ruff: noqa: E501
"""Delta sync runner (Epic 72 spec A5): reads only Talent OS-linked events through ``provider.list_changes`` and
applies the rules per change in its own savepoint. Provider sync tokens are used; a full (window) resync only when the
provider invalidates the token. Metered as ``connector_job`` (route group ``calendar_sync``)."""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar_sync import CalendarChannel
from app.models.interviews import Interview, InterviewParticipant
from app.services.calendar_sync import links, rules
from app.services.calendar_sync.common import now, settings_row, system_ctx, window
from app.services.connections.provider import EventChange, ProviderAuthError, ProviderError, SyncTokenInvalid


async def _candidate_ical_uids(db: AsyncSession, user_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, uuid.UUID | None, int]]:
    """ICalUIDs of upcoming scheduled Interviews where the user is the organiser or an internal participant ->
    (interview id, participant id, sequence)."""
    start, end = window()
    rows = (
        await db.execute(
            select(Interview.id, Interview.ical_uid, Interview.sequence, InterviewParticipant.id, InterviewParticipant.user_id, Interview.organizer_user_id)
            .join(InterviewParticipant, InterviewParticipant.interview_id == Interview.id)
            .where(
                Interview.status == "scheduled",
                Interview.starts_at >= start,
                Interview.starts_at <= end,
                InterviewParticipant.removed_at.is_(None),
                or_(InterviewParticipant.user_id == user_id, Interview.organizer_user_id == user_id),
            )
        )
    ).all()
    out: dict[str, tuple[uuid.UUID, uuid.UUID | None, int]] = {}
    for iid, ical, seq, pid, puid, _org in rows:
        if puid == user_id:
            out[ical] = (iid, pid, seq)
        else:
            out.setdefault(ical, (iid, None, seq))
    return out


async def run(db: AsyncSession, channel: CalendarChannel, *, full: bool = False) -> dict[str, Any]:
    from app.services import connections_service, metering_service

    known_links = await links.for_connection(db, channel.connection_id)
    by_id = {lk.provider_event_id: lk for lk in known_links}
    candidates = await _candidate_ical_uids(db, channel.user_id)
    known_ical = frozenset({lk.ical_uid for lk in known_links} | set(candidates))
    conn = await connections_service.get_active(db, channel.user_id, provider=channel.provider)
    if conn is None or conn.connection_id != channel.connection_id:
        from app.services.calendar_sync import channels

        await channels.mark_not_syncing(db, channel, "connection_inactive")
        return {"status": "not_syncing"}
    token = None if full else channel.sync_token
    try:
        try:
            changes, next_token = await conn.impl.list_changes(
                conn.token_set, token, known_ids=frozenset(by_id), known_ical_uids=known_ical, window=window()
            )
        except SyncTokenInvalid:
            changes, next_token = await conn.impl.list_changes(
                conn.token_set, None, known_ids=frozenset(by_id), known_ical_uids=known_ical, window=window()
            )
    except ProviderAuthError:
        await connections_service.mark_needs_reconnect(db, channel.connection_id, "calendar_sync_unauthorized")
        return {"status": "needs_reconnect"}
    except ProviderError as exc:
        channel.consecutive_failures += 1
        channel.last_error_code = type(exc).__name__[:60]
        await db.flush()
        logger.warning("calendar delta failed (channel={}, error={})", channel.channel_id, type(exc).__name__)
        return {"status": "error"}
    settings = await settings_row(db)
    outcomes: list[str] = []
    for change in changes:
        outcomes.append(await _one(db, channel, conn.provider, change, by_id, candidates, settings))
    channel.sync_token = next_token or channel.sync_token
    channel.last_sync_at = now()
    channel.consecutive_failures = 0
    channel.last_error_code = None
    channel.sync_requested_at = None
    await db.flush()
    await metering_service.record(system_ctx(channel.tenant_id), "connector_job", route_group="calendar_sync")
    return {"status": "ok", "changes": len(changes), "outcomes": outcomes}


async def _one(
    db: AsyncSession,
    channel: CalendarChannel,
    provider: str,
    change: EventChange,
    by_id: dict[str, Any],
    candidates: dict[str, tuple[uuid.UUID, uuid.UUID | None, int]],
    settings: Any,
) -> str:
    link = by_id.get(change.provider_event_id) or await links.by_event(db, channel.connection_id, change.provider_event_id)
    if link is None:
        hit = candidates.get(change.ical_uid or "")
        if hit is None or hit[1] is None or change.organizer_is_self:
            return "unlinked"
        link = await links.attendee_link(
            db, interview_id=hit[0], participant_id=hit[1], connection_id=channel.connection_id, provider=provider,
            provider_event_id=change.provider_event_id, ical_uid=change.ical_uid or "", sequence=hit[2],
        )  # fmt: skip
        by_id[change.provider_event_id] = link
    try:
        async with db.begin_nested():
            return await rules.apply(db, link, change, settings)
    except Exception:
        logger.exception("calendar rule failed (link={})", link.id)
        return "error"
