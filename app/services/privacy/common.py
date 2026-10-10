"""Shared helpers of the privacy package: clock, principal, admin notifications."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.core.context import RequestContext
from app.core.errors import Unauthorized
from app.core.mailer import EmailMessage, send_email


def now() -> datetime:
    return datetime.now(UTC)


def actor_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


def require_tenant(ctx: RequestContext) -> uuid.UUID:
    if ctx.tenant_id is None:
        raise Unauthorized()
    return ctx.tenant_id


async def email_users(
    users: Iterable[dict[str, Any]], subject: str, text: str, *, tenant_id: uuid.UUID | None, kind: str
) -> int:
    """Email each user (``{"email": ...}``). In-app notifications arrive with Epic 10 (activity/notify).

    Never raises: a mail failure is logged and the business change stands.
    """
    sent = 0
    for user in users:
        email = user.get("email")
        if not email:
            continue
        try:
            await send_email(EmailMessage(to=str(email), subject=subject, text=text, tenant_id=tenant_id, kind=kind))
            sent += 1
        except Exception:
            logger.exception("Sending {} email failed (tenant_id={})", kind, tenant_id)
    return sent
