# ruff: noqa: E501, D401
"""Signing secrets and request headers (Epic 68, §0.4, FR-351, NFR-63). The secret is ``whsec_<token>``, shown once,
stored in the vault (owner ``webhook_subscription``); rotation keeps ``signing_secret__previous`` for 24 h and every
delivery in the overlap carries both ``v1`` signatures. No credential, cookie or token is ever sent."""

import json
import secrets
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.settings import get_settings
from app.core.signing import signature_header
from app.services.webhooks.common import OWNER_TYPE, SECRET_NAME

ROTATION_OVERLAP_HOURS = 24
USER_AGENT_PATH = "/developers/docs/webhooks"


def new_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def _ref(subscription_id: uuid.UUID, name: str = SECRET_NAME) -> Any:
    from app.services import vault_service

    return vault_service.SecretRef(OWNER_TYPE, subscription_id, name)


async def store(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> str:
    from app.services import vault_service

    value = new_secret()
    await vault_service.put(ctx, db, _ref(subscription_id), value)
    return value


async def rotate(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> tuple[str, Any]:
    from app.services import vault_service

    value = new_secret()
    await vault_service.rotate(ctx, db, _ref(subscription_id), value, keep_previous_hours=ROTATION_OVERLAP_HOURS)
    from app.services.webhooks.common import now

    return value, now() + timedelta(hours=ROTATION_OVERLAP_HOURS)


async def signing_secrets(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, *, purpose: str = "webhook_sign") -> list[bytes]:
    """Current secret first, then the previous one while it has not expired (dual signature during rotation)."""
    from app.services import vault_service

    out: list[bytes] = []
    for name in (SECRET_NAME, SECRET_NAME + vault_service.PREVIOUS_SUFFIX):
        value = await vault_service.get(ctx, db, _ref(subscription_id, name), purpose=purpose)
        if value:
            out.append(value.encode("utf-8"))
    return out


async def delete_all(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> None:
    from app.services import vault_service

    await vault_service.delete_owner(ctx, db, OWNER_TYPE, subscription_id)


def canonical(obj: dict[str, Any]) -> bytes:
    """The same serializer as ``public_events_service.envelope`` (sorted keys, compact, UTF-8)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def user_agent() -> str:
    host = get_settings().frontend_url.rstrip("/")
    return f"TalentOS-Webhooks/1.0 (+{host}{USER_AGENT_PATH})"


def headers(
    secrets_: list[bytes], body: bytes, *, event_id: uuid.UUID | str, event_type: str, delivery_id: uuid.UUID | str | None,
    attempt: int, replay: bool = False, test: bool = False, timestamp: int | None = None,
) -> dict[str, str]:
    """The fixed header set of every POST (§0.4). ``secrets_`` must not be empty."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    out = {
        "Content-Type": "application/json",
        "User-Agent": user_agent(),
        "TalentOS-Signature": signature_header(secrets_, ts, body),
        "TalentOS-Event-Id": str(event_id),
        "TalentOS-Event-Type": event_type,
        "TalentOS-Attempt": str(attempt),
    }
    if delivery_id is not None:
        out["TalentOS-Delivery-Id"] = str(delivery_id)
    if replay:
        out["TalentOS-Replay"] = "true"
    if test:
        out["TalentOS-Test"] = "true"
    return out


__all__ = ["canonical", "delete_all", "headers", "new_secret", "rotate", "signing_secrets", "store", "user_agent"]
