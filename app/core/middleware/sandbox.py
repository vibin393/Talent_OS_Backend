"""Sandbox response header + activity stamp (Epic 66, Story 66.9, FR-339, AD-85).

When the request's tenant (``RequestContext.tenant_id`` as set by authentication, read when the response starts) is a
Sandbox Tenant, the response carries ``X-TalentOS-Sandbox: true`` and ``tenants.sandbox_last_active_at`` is touched at
most once an hour per process (in-process throttle; one platform-table UPDATE). Unauthenticated requests (no tenant on
the context) never carry the header.
"""

import time
import uuid

from loguru import logger
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import get_context
from app.core.tenancy import is_sandbox

SANDBOX_HEADER = "X-TalentOS-Sandbox"
_HEADER = (SANDBOX_HEADER.lower().encode("latin-1"), b"true")
TOUCH_INTERVAL_SECONDS = 3600.0
_last_touch: dict[str, float] = {}


def reset_touch_throttle() -> None:
    _last_touch.clear()


async def touch_activity(tenant_id: uuid.UUID | str) -> bool:
    """Stamp ``sandbox_last_active_at`` unless this process did so within the last hour; True when written."""
    key = str(tenant_id)
    now = time.monotonic()
    last = _last_touch.get(key)
    if last is not None and now - last < TOUCH_INTERVAL_SECONDS:
        return False
    _last_touch[key] = now
    from app.core.database import platform_session

    try:
        async with platform_session() as db:
            await db.execute(
                text(
                    "UPDATE tenants SET sandbox_last_active_at = now() "
                    "WHERE id = CAST(:t AS uuid) AND mode = 'sandbox'"
                ),
                {"t": key},
            )
    except Exception:
        logger.exception("Could not stamp sandbox activity (tenant_id={})", key)
        return False
    return True


class SandboxHeaderMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_flag(message: Message) -> None:
            if message["type"] == "http.response.start":
                tenant_id = get_context().tenant_id
                if tenant_id is not None:
                    try:
                        sandbox = await is_sandbox(tenant_id)
                    except Exception:
                        logger.exception("Sandbox mode lookup failed (tenant_id={})", tenant_id)
                        sandbox = False
                    if sandbox:
                        message.setdefault("headers", [])
                        message["headers"] = [*message["headers"], _HEADER]
                        await touch_activity(tenant_id)
            await send(message)

        await self.app(scope, receive, send_with_flag)
