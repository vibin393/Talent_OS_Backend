"""Epic 10 mailbox hook implemented by Epic 7's connections (``comms_service.mailbox_provider`` discovers it).

* ``get_send_connection(ctx, user_id)`` -> the user's active connection (refreshed) or None.
* ``send_mail(conn, message)`` -> ``SentRef`` (Epic 33: provider message id + thread ref; a bare ``str`` result
  of an older adapter is wrapped); raises ``ConnectionExpired`` when the provider refuses
  the credentials (the connection is marked ``needs_reconnect`` and comms falls back to the platform mailer).
"""

import uuid

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.mailer import EmailMessage
from app.services import connections_service
from app.services.connections.provider import ConnectionExpired, ProviderAuthError, SentRef


async def get_send_connection(
    ctx: RequestContext, user_id: uuid.UUID
) -> connections_service.ActiveConnection | None:
    if ctx.tenant_id is None:
        return None
    async with tenant_session(ctx.tenant_id) as db:
        return await connections_service.get_active(db, user_id)


async def send_mail(conn: connections_service.ActiveConnection, message: EmailMessage) -> SentRef | None:
    try:
        result = await conn.impl.send_mail(conn.token_set, message)
        if result is None or isinstance(result, SentRef):
            return result
        return SentRef(str(result))
    except ProviderAuthError as exc:
        if message.tenant_id is not None:
            async with tenant_session(message.tenant_id) as db:
                await connections_service.mark_needs_reconnect(db, conn.connection_id, "send_unauthorized")
        raise ConnectionExpired("mailbox credentials refused") from exc


__all__ = ["ConnectionExpired", "SentRef", "get_send_connection", "send_mail"]
