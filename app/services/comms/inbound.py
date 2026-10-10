# ruff: noqa: E501
"""Inbound email capture (Phase 2 Epic 33, FR-171): replies to Talent OS-originated threads only.

* ``record_thread`` -- called by ``comms_service.deliver`` after a mailbox send: when the message is filed under
  records (``related``) and the sender's connection holds the reply-capture scopes, the provider thread is
  remembered in ``email_threads`` (``sync_until`` = sent + ``thread_sync_days``).
* ``sync_threads`` -- the ``comms.sync_replies`` beat (every 2 minutes, queue ``connectors``) reads each due
  thread through ``CalendarProvider.list_thread_messages`` (that one thread, nothing else). A message counts as a
  reply when it is not authored by the connected account and its provider id is unseen; each reply becomes an
  ``inbound_emails`` row (text only, HTML sanitised to text, <= 100 KB, attachment names only) plus an
  ``email_reply`` Activity on every related record; ``EmailReplyReceived`` notifies the sender and feeds the
  ``comms.email_reply_received`` workflow trigger.
* Errors: ``ProviderAuthError`` -> connection ``needs_reconnect`` and the threads ``error``; other provider
  errors back off (2 min x 2^n, at most 1 h) and retry on a later cycle.
* Reads: ``get_thread`` (P1 email read rules), ``reply_capture_status`` / ``disable_reply_capture`` (the
  caller's own connection).

Never logs addresses, subjects or bodies.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.core.permissions import Principal
from app.events.comms import EmailReplyReceived
from app.models.comms import Email, EmailRecipient, EmailThread, InboundEmail
from app.models.connections import UserConnection
from app.schemas.messaging import EmailThreadOut, InboundEmailOut, ReplyCaptureStatusOut
from app.services.comms import channel_settings
from app.services.comms.html import html_to_text, sanitize_html

SYNC_INTERVAL_SECONDS = 120  # beat cadence (``comms-sync-replies``)
LATENCY_TARGET_SECONDS = 300  # FR-171: a reply is on the record within 5 minutes
MAX_BODY_CHARS = 100_000
MAX_BACKOFF = timedelta(hours=1)
REPLY_ENTITY_TYPES = ("candidate", "contact", "client", "lead", "opportunity", "invoice")


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _handle_info(send_conn: Any) -> tuple[uuid.UUID, str, tuple[str, ...], str | None] | None:
    """``(connection_id, provider, scopes, impl name)`` of a comms ``SendConnection`` wrapping Epic 7's
    ``ActiveConnection`` (None for other mailbox adapters, e.g. test doubles)."""
    handle = getattr(send_conn, "handle", None)
    connection_id = getattr(handle, "connection_id", None)
    token_set = getattr(handle, "token_set", None)
    if connection_id is None or token_set is None:
        return None
    impl = getattr(handle, "impl", None)
    provider = str(getattr(handle, "provider", "") or getattr(send_conn, "provider", ""))
    return connection_id, provider, tuple(getattr(token_set, "scopes", ()) or ()), getattr(impl, "name", None)


async def record_thread(db: AsyncSession, message: Email, send_conn: Any, sent_ref: Any) -> uuid.UUID | None:
    """Remember the provider thread of a mailbox-sent ``message`` (no-op unless filed under records -- its
    ``related`` refs plus the candidate / contact recipients --, the tenant
    allows reply capture and the connection holds the reply-capture scopes)."""
    from app.services.connections.provider import has_reply_scopes

    thread_ref = getattr(sent_ref, "thread_ref", None)
    related = [
        {"entity_type": str(r["entity_type"]), "entity_id": str(r["entity_id"])}
        for r in (message.related or [])
        if isinstance(r, dict) and r.get("entity_type") and r.get("entity_id")
    ]
    recipients = await db.execute(
        select(EmailRecipient.entity_type, EmailRecipient.entity_id).where(
            EmailRecipient.message_id == message.id, EmailRecipient.entity_id.is_not(None)
        )
    )
    for entity_type, entity_id in recipients.all():
        ref = {"entity_type": str(entity_type), "entity_id": str(entity_id)}
        if ref not in related:
            related.append(ref)
    info = _handle_info(send_conn)
    if not thread_ref or not related or info is None or message.sender_user_id is None:
        return None
    connection_id, provider, scopes, impl_name = info
    if not has_reply_scopes(provider, scopes, impl_name):
        return None
    settings = await channel_settings.get_row(db)
    if not settings.inbound_sync_enabled:
        return None
    sent_at = message.sent_at or _now()
    thread_id = (
        await db.execute(
            pg_insert(EmailThread)
            .values(
                id=uuid.uuid4(),
                tenant_id=_tenant(db),
                email_message_id=message.id,
                user_id=message.sender_user_id,
                connection_id=connection_id,
                provider=provider,
                thread_ref=str(thread_ref)[:255],
                rfc_message_id=(getattr(sent_ref, "rfc_message_id", None) or None),
                related=related,
                sync_until=sent_at + timedelta(days=settings.thread_sync_days),
                status="active",
                next_sync_at=sent_at,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "provider", "thread_ref", "user_id"])
            .returning(EmailThread.id)
        )
    ).scalar_one_or_none()
    return thread_id


# ================================================================ sync (beat body)
def _reply_text(body_text: str, body_html: str | None) -> str:
    text = (body_text or "").strip()
    if not text and body_html:
        text = html_to_text(sanitize_html(body_html))
    return text[:MAX_BODY_CHARS]


def _backoff(error_count: int) -> timedelta:
    return min(timedelta(seconds=SYNC_INTERVAL_SECONDS * 2 ** max(error_count - 1, 0)), MAX_BACKOFF)


async def _file_reply(db: AsyncSession, thread: EmailThread, msg: Any) -> uuid.UUID | None:
    from app.services import activity_service

    inserted = (
        await db.execute(
            pg_insert(InboundEmail)
            .values(
                id=uuid.uuid4(),
                tenant_id=thread.tenant_id,
                thread_id=thread.id,
                email_message_id=thread.email_message_id,
                provider_message_id=str(msg.provider_message_id)[:255],
                from_address=(msg.from_address or "")[:320],
                subject=(msg.subject or "")[:500],
                body_text=_reply_text(msg.body_text, msg.body_html),
                received_at=msg.sent_at or _now(),
                attachment_names=[str(n)[:255] for n in msg.attachment_names or ()][:50],
                related=list(thread.related or []),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "provider_message_id"])
            .returning(InboundEmail.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return None
    actx = RequestContext(tenant_id=thread.tenant_id, principal=None, actor_kind="system")
    subject = (msg.subject or "")[:200]
    first: dict[str, uuid.UUID] = {}
    for ref in thread.related or []:
        entity_type = str(ref.get("entity_type"))
        try:
            entity_id = uuid.UUID(str(ref.get("entity_id")))
        except ValueError:
            continue
        first.setdefault(entity_type, entity_id)
        await activity_service.add_system_entry(
            actx,
            db,
            entity_type,
            entity_id,
            f'Email reply from {msg.from_address}: "{subject}"',
            {
                "inbound_email_id": str(inserted),
                "email_message_id": str(thread.email_message_id),
                "from_address": msg.from_address,
                "subject": subject,
            },
            kind="email_reply",
            occurred_at=msg.sent_at,
        )
    emit(
        db,
        EmailReplyReceived(
            tenant_id=thread.tenant_id,
            inbound_email_id=inserted,
            email_message_id=thread.email_message_id,
            thread_id=thread.id,
            sender_user_id=thread.user_id,
            candidate_id=first.get("candidate"),
            contact_id=first.get("contact"),
        ),
    )
    return inserted


async def _sync_group(
    db: AsyncSession, row: UserConnection, threads: list[EmailThread], now: datetime
) -> dict[str, int]:
    from app.services import connections_service
    from app.services.connections.provider import ProviderAuthError, ProviderError, has_reply_scopes

    counts = {"threads": 0, "replies": 0, "errors": 0}
    conn = await connections_service.get_active(db, row.user_id, row.provider)
    if conn is None or conn.connection_id != row.id:
        for t in threads:
            t.status, t.next_sync_at = "error", None
        counts["errors"] += len(threads)
        return counts
    if not has_reply_scopes(row.provider, conn.token_set.scopes, getattr(conn.impl, "name", None)):
        for t in threads:
            t.status = "stopped"
        return counts
    account = (conn.account_email or "").lower()
    own_ids = {
        mid
        for mid in (
            await db.execute(
                select(Email.provider_message_id).where(Email.id.in_([t.email_message_id for t in threads]))
            )
        ).scalars()
        if mid
    }
    for thread in threads:
        try:
            messages = await conn.impl.list_thread_messages(
                conn.token_set, thread.thread_ref, thread.last_seen_ref
            )
        except ProviderAuthError:
            await connections_service.mark_needs_reconnect(db, row.id, "reply_sync_unauthorized")
            for t in threads:
                t.status, t.next_sync_at = "error", None
            counts["errors"] += 1
            return counts
        except ProviderError as exc:
            thread.error_count += 1
            thread.next_sync_at = now + _backoff(thread.error_count)
            counts["errors"] += 1
            logger.warning("Reply sync failed (thread={}, error={})", thread.id, type(exc).__name__)
            continue
        counts["threads"] += 1
        for msg in messages:
            if not msg.provider_message_id or msg.provider_message_id in own_ids:
                continue
            if account and (msg.from_address or "").lower() == account:
                continue  # authored by the connected account itself
            if await _file_reply(db, thread, msg) is not None:
                counts["replies"] += 1
        if messages:
            thread.last_seen_ref = str(messages[-1].provider_message_id)[:255]
        thread.last_synced_at = now
        thread.error_count = 0
        thread.next_sync_at = now + timedelta(seconds=SYNC_INTERVAL_SECONDS)
    return counts


async def sync_threads(ctx: RequestContext, db: AsyncSession, now: datetime | None = None) -> dict[str, int]:  # noqa: ARG001
    """One reply-sync cycle for the session's tenant."""
    now = now or _now()
    totals = {"threads": 0, "replies": 0, "errors": 0, "stopped": 0}
    settings = await channel_settings.get_row(db)
    if not settings.inbound_sync_enabled:
        return totals
    expired = list(
        (
            await db.execute(
                select(EmailThread).where(EmailThread.status == "active", EmailThread.sync_until <= now)
            )
        ).scalars()
    )
    for t in expired:
        t.status = "stopped"
    totals["stopped"] = len(expired)
    due = list(
        (
            await db.execute(
                select(EmailThread)
                .where(
                    EmailThread.status == "active",
                    EmailThread.sync_until > now,
                    (EmailThread.next_sync_at.is_(None)) | (EmailThread.next_sync_at <= now),
                )
                .order_by(EmailThread.connection_id, EmailThread.created_at)
                .limit(2000)
            )
        ).scalars()
    )
    groups: dict[uuid.UUID, list[EmailThread]] = defaultdict(list)
    for t in due:
        groups[t.connection_id].append(t)
    for connection_id, threads in groups.items():
        row = await db.get(UserConnection, connection_id)
        if row is None or row.status != "active":
            for t in threads:
                t.status, t.next_sync_at = "error", None
            totals["errors"] += len(threads)
            continue
        counts = await _sync_group(db, row, threads, now)
        for key, value in counts.items():
            totals[key] += value
    await db.flush()
    return totals


# ================================================================ reads
def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise NotFound("Resource not found")
    return principal


async def get_thread(ctx: RequestContext, db: AsyncSession, email_id: uuid.UUID) -> EmailThreadOut:
    """The original email (P1 read rules: the sender or a reader of a related record) and its captured replies."""
    from app.services import comms_service

    detail = await comms_service.get_email(ctx, db, email_id)
    replies = (
        await db.execute(
            select(InboundEmail)
            .where(InboundEmail.email_message_id == email_id)
            .order_by(InboundEmail.received_at)
        )
    ).scalars()
    data = detail.model_dump()
    data.pop("body_html", None)
    from app.schemas.comms import EmailOut

    return EmailThreadOut(
        email=EmailOut.model_validate(data), replies=[InboundEmailOut.model_validate(r) for r in replies]
    )


async def _my_connection(db: AsyncSession, user_id: uuid.UUID) -> UserConnection | None:
    return (
        await db.execute(
            select(UserConnection)
            .where(UserConnection.user_id == user_id, UserConnection.status != "revoked")
            .order_by(UserConnection.connected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def reply_capture_status(ctx: RequestContext, db: AsyncSession) -> ReplyCaptureStatusOut:
    from app.services.connections.provider import has_reply_scopes

    principal = _principal(ctx)
    settings = await channel_settings.get_row(db)
    row = await _my_connection(db, principal.id)
    granted = row is not None and has_reply_scopes(row.provider, row.scopes)
    tracked, last = (
        await db.execute(
            select(func.count(EmailThread.id), func.max(EmailThread.last_synced_at)).where(
                EmailThread.user_id == principal.id, EmailThread.status == "active"
            )
        )
    ).one()
    return ReplyCaptureStatusOut(
        enabled=bool(
            granted and row is not None and row.status == "active" and settings.inbound_sync_enabled
        ),
        provider=row.provider if row is not None else None,
        connection_id=row.id if row is not None else None,
        scopes_granted=granted,
        threads_tracked=int(tracked or 0),
        last_synced_at=last,
        tenant_enabled=settings.inbound_sync_enabled,
    )


async def disable_reply_capture(ctx: RequestContext, db: AsyncSession) -> ReplyCaptureStatusOut:
    """Stop capturing: the reply scopes are dropped from the stored grant (no new threads) and the user's
    threads stop. Re-enabling goes through the OAuth consent again."""
    from app.core import audit
    from app.services.connections.provider import REPLY_SCOPES

    principal = _principal(ctx)
    reply = {s.lower() for scopes in REPLY_SCOPES.values() for s in scopes}
    for row in (
        await db.execute(select(UserConnection).where(UserConnection.user_id == principal.id))
    ).scalars():
        kept = [s for s in row.scopes or [] if s.lower() not in reply]
        if kept != list(row.scopes or []):
            row.scopes = kept
            audit.record(
                "connection.reply_capture_disabled",
                "user_connection",
                row.id,
                session=db,
                provider=row.provider,
            )
    for t in (
        await db.execute(
            select(EmailThread).where(EmailThread.user_id == principal.id, EmailThread.status == "active")
        )
    ).scalars():
        t.status = "stopped"
    await db.flush()
    return await reply_capture_status(ctx, db)
