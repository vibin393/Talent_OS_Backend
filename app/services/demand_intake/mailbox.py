# ruff: noqa: E501
"""Connected-mailbox intake (Epic 55, spec 0.3, coordinator decision C-3).

``poll_connected_mailbox(ctx, db)`` -- every 2 minutes per tenant with ``intake_mode=connected_mailbox``: the
designated User's connected Google / Microsoft mailbox (P1 Epic 7 / P2 FR-171 connections) is read **only** in the
configured folder / label ("Talent OS Intake") through the read-only provider method ``list_folder_messages`` (reply
capture read scopes). The settings row is locked ``FOR UPDATE SKIP LOCKED`` (one poller per tenant); the cursor is the
ISO timestamp of the newest message seen. Missing scopes / a revoked connection -> ``mailbox_status`` warning.
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demand_intake import DemandAgentSettings
from app.services.demand_intake import receive

POLL_LIMIT = 50


async def poll_connected_mailbox(ctx: RequestContext, db: AsyncSession) -> int:
    """Ingest new messages of the intake folder; returns the number of new messages stored."""
    from app.services import connections_service
    from app.services.connections.provider import ProviderAuthError, ProviderError, has_reply_scopes

    settings = (
        await db.execute(select(DemandAgentSettings).with_for_update(skip_locked=True))
    ).scalar_one_or_none()
    if settings is None or settings.intake_mode != "connected_mailbox" or settings.mailbox_user_id is None:
        return 0
    conn = await connections_service.get_active(db, settings.mailbox_user_id)
    if conn is None:
        settings.mailbox_status = "not_connected"
        await db.flush()
        return 0
    if not has_reply_scopes(conn.provider, conn.token_set.scopes, getattr(conn.impl, "name", None)):
        settings.mailbox_status = "needs_reconnect"
        await db.flush()
        return 0
    lister = getattr(conn.impl, "list_folder_messages", None)
    if lister is None:
        settings.mailbox_status = "needs_reconnect"
        await db.flush()
        return 0
    try:
        messages = await lister(conn.token_set, settings.mailbox_folder, settings.mailbox_cursor, POLL_LIMIT)
    except ProviderAuthError:
        await connections_service.mark_needs_reconnect(db, conn.connection_id, "intake_read_refused")
        settings.mailbox_status = "needs_reconnect"
        await db.flush()
        return 0
    except ProviderError as exc:
        logger.warning("Demand intake mailbox poll failed: {}", type(exc).__name__)
        return 0
    settings.mailbox_status = "connected"
    stored = 0
    cursor = settings.mailbox_cursor
    for item in sorted(messages, key=lambda m: m.received_at):
        stamp = item.received_at.isoformat()
        if cursor is None or stamp > cursor:
            cursor = stamp
        try:
            parsed = receive.parse_mime(item.raw)
        except Exception:
            logger.warning("Demand intake: a mailbox message could not be parsed")
            continue
        _, duplicate = await receive.ingest(
            ctx, db, settings, source="connected_mailbox", parsed=parsed, raw=item.raw,
            external_ref=(item.provider_message_id or parsed.message_id or "")[:300] or None,
            created_by_kind="system", created_by_id=None,
        )  # fmt: skip
        stored += int(not duplicate)
    settings.mailbox_cursor = cursor
    await db.flush()
    return stored


__all__ = ["POLL_LIMIT", "poll_connected_mailbox"]
