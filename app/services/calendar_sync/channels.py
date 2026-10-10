# ruff: noqa: E501
"""Push channel lifecycle (Epic 72 story 72.3, spec A4): create, renew, stop and the fallback polling schedule.

* ``ensure_channel(db, connection_id)`` -- when the tenant turned sync on, the connection is active and its user
  organises / joins a scheduled Interview in the next 90 days: a random channel secret goes to the vault (owner
  ``calendar_connection``, name ``channel_token``), the channel row + platform lookup are written, ``provider.watch``
  is called and an initial delta obtains the sync token. A watch failure leaves the channel ``polling``.
* ``renew_due`` -- Google: watch a new channel, switch, stop the old one; Microsoft / renewable: extend in place.
  Failure -> ``polling`` with a daily retry.
* ``poll_due`` -- ``polling`` channels (and ``active`` ones silent for 6 h) whose ``next_poll_at`` passed.
* ``stop_for_connection`` -- connection revoked / needs reconnect: stop watching, drop the secret and the lookup,
  and tell the user (Task) when upcoming Interviews are affected.
"""

import secrets
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.database import platform_session
from app.core.events import emit
from app.core.settings import get_settings
from app.events.calendar_sync import CalendarChannelStateChanged
from app.models.calendar_sync import LIVE_CHANNEL_STATUSES, CalendarChannel, CalendarChannelLookup
from app.models.connections import UserConnection
from app.models.interviews import Interview, InterviewParticipant
from app.services.calendar_sync.common import (
    CHANNEL_SECRET,
    GOOGLE_TTL,
    MICROSOFT_TTL,
    RENEW_MARGIN,
    SAFETY_POLL_AFTER,
    VAULT_OWNER,
    now,
    open_task,
    settings_row,
    system_ctx,
    tenant_of,
)
from app.services.connections.provider import ChannelRef, ProviderError


def channel_url(channel_id: uuid.UUID) -> str:
    return f"{get_settings().p5_public_webhook_base_url}/api/v1/hooks/calendar/{channel_id}"


def _ttl(provider: str) -> timedelta:
    return MICROSOFT_TTL if provider == "microsoft" else GOOGLE_TTL


def _set_status(db: AsyncSession, ch: CalendarChannel, status: str) -> None:
    if ch.status != status:
        ch.status = status
        emit(db, CalendarChannelStateChanged(tenant_id=ch.tenant_id, channel_id=ch.channel_id, connection_id=ch.connection_id, status=status))


async def live_for(db: AsyncSession, connection_id: uuid.UUID) -> CalendarChannel | None:
    return (
        await db.execute(
            select(CalendarChannel).where(
                CalendarChannel.connection_id == connection_id, CalendarChannel.status.in_(LIVE_CHANNEL_STATUSES)
            )
        )
    ).scalar_one_or_none()


async def by_channel_id(db: AsyncSession, channel_id: uuid.UUID) -> CalendarChannel | None:
    return (await db.execute(select(CalendarChannel).where(CalendarChannel.channel_id == channel_id))).scalar_one_or_none()


async def upcoming_interviews(db: AsyncSession, user_id: uuid.UUID, days: int = 90) -> list[uuid.UUID]:
    at = now()
    rows = (
        await db.execute(
            select(Interview.id)
            .outerjoin(InterviewParticipant, InterviewParticipant.interview_id == Interview.id)
            .where(
                Interview.status == "scheduled",
                Interview.ends_at >= at,
                Interview.starts_at <= at + timedelta(days=days),
                or_(
                    Interview.organizer_user_id == user_id,
                    (InterviewParticipant.user_id == user_id) & InterviewParticipant.removed_at.is_(None),
                ),
            )
        )
    ).scalars()
    return list(dict.fromkeys(rows))


async def _write_lookup(channel_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await pdb.execute(
            pg_insert(CalendarChannelLookup).values(channel_id=channel_id, tenant_id=tenant_id).on_conflict_do_nothing(index_elements=["channel_id"])
        )


async def _drop_lookup(channel_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await pdb.execute(delete(CalendarChannelLookup).where(CalendarChannelLookup.channel_id == channel_id))


async def tenant_for(channel_id: uuid.UUID) -> uuid.UUID | None:
    async with platform_session() as pdb:
        return (
            await pdb.execute(select(CalendarChannelLookup.tenant_id).where(CalendarChannelLookup.channel_id == channel_id))
        ).scalar_one_or_none()


async def channel_secret(db: AsyncSession, connection_id: uuid.UUID, *, purpose: str = "calendar_channel_verify") -> str | None:
    from app.services import vault_service

    ref = vault_service.SecretRef(VAULT_OWNER, connection_id, CHANNEL_SECRET)
    return await vault_service.get(system_ctx(tenant_of(db)), db, ref, purpose=purpose)


async def _secret_for(db: AsyncSession, connection_id: uuid.UUID) -> str:
    from app.services import vault_service

    existing = await channel_secret(db, connection_id, purpose="calendar_channel_watch")
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    ref = vault_service.SecretRef(VAULT_OWNER, connection_id, CHANNEL_SECRET)
    await vault_service.put(system_ctx(tenant_of(db)), db, ref, token)
    return token


async def _watch(db: AsyncSession, ch: CalendarChannel, conn: Any, token: str) -> None:
    """Create the provider channel for ``ch``; failure -> ``polling``."""
    settings = await settings_row(db)
    try:
        ref = await conn.impl.watch(conn.token_set, channel_url(ch.channel_id), str(ch.channel_id), token, int(_ttl(ch.provider).total_seconds()))
    except ProviderError as exc:
        _set_status(db, ch, "polling")
        ch.consecutive_failures += 1
        ch.last_error_code = type(exc).__name__[:60]
        ch.next_poll_at = now() + timedelta(minutes=settings.poll_minutes)
        ch.renew_after = now() + timedelta(hours=24)  # daily re-watch
        await db.flush()
        return
    _set_status(db, ch, "active")
    ch.provider_resource_id = ref.resource_id
    ch.expires_at = ref.expires_at
    ch.renew_after = (ref.expires_at - RENEW_MARGIN.get(ch.provider, timedelta(hours=24))) if ref.expires_at else now() + timedelta(days=1)
    ch.next_poll_at = now() + SAFETY_POLL_AFTER
    ch.consecutive_failures = 0
    ch.last_error_code = None
    await db.flush()


async def ensure_channel(db: AsyncSession, connection_id: uuid.UUID) -> CalendarChannel | None:
    from app.services import connections_service
    from app.services.calendar_sync import delta

    settings = await settings_row(db)
    if not settings.enabled:
        return None
    row = await db.get(UserConnection, connection_id)
    if row is None or row.status != "active":
        return None
    live = await live_for(db, connection_id)
    if live is not None:
        return live
    if not await upcoming_interviews(db, row.user_id):
        return None
    conn = await connections_service.get_active(db, row.user_id, provider=row.provider)
    if conn is None or conn.connection_id != connection_id:
        return None
    token = await _secret_for(db, connection_id)
    ch = CalendarChannel(
        id=uuid.uuid4(), tenant_id=tenant_of(db), connection_id=connection_id, user_id=row.user_id,
        provider=row.provider if row.provider in ("google", "microsoft") else "fake", channel_id=uuid.uuid4(),
        status="active", consecutive_failures=0,
    )  # fmt: skip
    db.add(ch)
    await db.flush()
    await _write_lookup(ch.channel_id, ch.tenant_id)
    await _watch(db, ch, conn, token)
    audit.record("calendar_sync.channel_created", "calendar_channel", ch.id, session=db, provider=ch.provider, status=ch.status)
    await delta.run(db, ch, full=True)  # initial listing -> sync token (linked events only)
    return ch


async def renew(db: AsyncSession, ch: CalendarChannel) -> str:
    from app.services import connections_service

    conn = await connections_service.get_active(db, ch.user_id, provider=ch.provider)
    if conn is None or conn.connection_id != ch.connection_id:
        await mark_not_syncing(db, ch, "connection_inactive")
        return "not_syncing"
    settings = await settings_row(db)
    ref = ChannelRef(str(ch.channel_id), ch.provider_resource_id, ch.expires_at)
    if ch.status == "active" and (ch.provider == "microsoft" or getattr(conn.impl, "renewable", False)):
        _set_status(db, ch, "renewing")
        try:
            new_ref = await conn.impl.renew_watch(conn.token_set, ref, int(_ttl(ch.provider).total_seconds()))
        except ProviderError:
            return await _renew_failed(db, ch, settings.poll_minutes)
        _set_status(db, ch, "active")
        ch.expires_at = new_ref.expires_at
        ch.renew_after = (new_ref.expires_at - RENEW_MARGIN.get(ch.provider, timedelta(hours=24))) if new_ref.expires_at else now() + timedelta(days=1)
        await db.flush()
        return "renewed"
    # Google-like: a new channel id, then stop the old one
    token = await _secret_for(db, ch.connection_id)
    old_channel_id, old_ref = ch.channel_id, ref
    new_id = uuid.uuid4()
    try:
        new_ref = await conn.impl.watch(conn.token_set, channel_url(new_id), str(new_id), token, int(_ttl(ch.provider).total_seconds()))
    except ProviderError:
        return await _renew_failed(db, ch, settings.poll_minutes)
    await _write_lookup(new_id, ch.tenant_id)
    ch.channel_id = new_id
    ch.provider_resource_id = new_ref.resource_id
    ch.expires_at = new_ref.expires_at
    ch.renew_after = (new_ref.expires_at - RENEW_MARGIN.get(ch.provider, timedelta(hours=24))) if new_ref.expires_at else now() + timedelta(days=1)
    _set_status(db, ch, "active")
    ch.consecutive_failures = 0
    await db.flush()
    if old_ref.resource_id:
        try:
            await conn.impl.stop_watch(conn.token_set, old_ref)
        except ProviderError:
            logger.warning("calendar: stopping the previous channel failed (channel={})", old_channel_id)
    await _drop_lookup(old_channel_id)
    return "rewatched"


async def _renew_failed(db: AsyncSession, ch: CalendarChannel, poll_minutes: int) -> str:
    _set_status(db, ch, "polling")
    ch.consecutive_failures += 1
    ch.last_error_code = "renew_failed"
    ch.next_poll_at = now() + timedelta(minutes=poll_minutes)
    ch.renew_after = now() + timedelta(hours=24)
    await db.flush()
    return "polling"


async def renew_due(db: AsyncSession) -> int:
    """Beat body (every 15 minutes): renew / re-watch channels whose ``renew_after`` passed."""
    at = now()
    rows = list(
        (
            await db.execute(
                select(CalendarChannel)
                .where(CalendarChannel.status.in_(("active", "polling")), CalendarChannel.renew_after <= at)
                .limit(200)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for ch in rows:
        await renew(db, ch)
    return len(rows)


async def poll_targets(db: AsyncSession) -> list[CalendarChannel]:
    at = now()
    return list(
        (
            await db.execute(
                select(CalendarChannel)
                .where(CalendarChannel.status.in_(("active", "polling")), CalendarChannel.next_poll_at <= at)
                .limit(200)
            )
        ).scalars()
    )


async def poll_due(db: AsyncSession) -> int:
    """Beat body (every minute): delta for ``polling`` channels and silent ``active`` ones (safety net)."""
    from app.services.calendar_sync import delta

    settings = await settings_row(db)
    if not settings.enabled:
        return 0
    count = 0
    for ch in await poll_targets(db):
        step = timedelta(minutes=settings.poll_minutes) if ch.status == "polling" else SAFETY_POLL_AFTER
        ch.next_poll_at = now() + step
        await delta.run(db, ch)
        count += 1
    await db.flush()
    return count


async def stop(db: AsyncSession, ch: CalendarChannel, status: str = "stopped", *, call_provider: bool = True) -> None:
    from app.services import connections_service

    if call_provider and ch.status in LIVE_CHANNEL_STATUSES and ch.provider_resource_id:
        conn = await connections_service.get_active(db, ch.user_id, provider=ch.provider)
        if conn is not None:
            try:
                await conn.impl.stop_watch(conn.token_set, ChannelRef(str(ch.channel_id), ch.provider_resource_id, ch.expires_at))
            except ProviderError:
                logger.warning("calendar: stop_watch failed (channel={})", ch.channel_id)
    _set_status(db, ch, status)
    ch.next_poll_at = None
    ch.renew_after = None
    await db.flush()
    await _drop_lookup(ch.channel_id)
    audit.record("calendar_sync.channel_stopped", "calendar_channel", ch.id, session=db, status=status)


async def mark_not_syncing(db: AsyncSession, ch: CalendarChannel, reason: str) -> None:
    ch.last_error_code = reason[:60]
    await stop(db, ch, "stopped", call_provider=False)
    await notify_not_syncing(db, ch.user_id)


async def notify_not_syncing(db: AsyncSession, user_id: uuid.UUID) -> None:
    """One Task per user (deduplicated) when upcoming Interviews (30 days) lose their calendar sync."""
    if not await upcoming_interviews(db, user_id, days=30):
        return
    await open_task(
        db, owner_id=user_id, title="Reconnect your calendar so interview changes reach Talent OS",
        notes="Your calendar connection stopped working. Reconnect it in Settings → Connections (/settings/connections).",
        entity_id=user_id,
    )  # fmt: skip


async def stop_for_connection(db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID, status: str) -> None:
    """``connections_service`` listener body for ``needs_reconnect`` / ``revoked``."""
    from app.services import vault_service

    ch = await live_for(db, connection_id)
    if ch is not None:
        await stop(db, ch, "stopped", call_provider=False)
    if status == "revoked":
        await vault_service.delete(system_ctx(tenant_of(db)), db, vault_service.SecretRef(VAULT_OWNER, connection_id, CHANNEL_SECRET))
    await notify_not_syncing(db, user_id)


async def stop_idle(db: AsyncSession) -> int:
    """Channels whose user has no upcoming Interview (7 days of idleness is approximated by 'none upcoming')."""
    count = 0
    for ch in list((await db.execute(select(CalendarChannel).where(CalendarChannel.status.in_(LIVE_CHANNEL_STATUSES)))).scalars()):
        if not await upcoming_interviews(db, ch.user_id) and ch.last_notification_at is not None and ch.last_notification_at < now() - timedelta(days=7):
            await stop(db, ch)
            count += 1
    return count


async def connections_to_backfill(db: AsyncSession) -> list[uuid.UUID]:
    return list((await db.execute(select(UserConnection.id).where(UserConnection.status == "active"))).scalars())
