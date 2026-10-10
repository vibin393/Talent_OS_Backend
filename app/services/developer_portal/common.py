# ruff: noqa: E501, D401
"""Shared helpers of the Developer Portal (Epic 74): clock, platform contexts, the org role matrix (spec 0.1), the
short-lived key-value store (Redis db 2; in-process when ``RATE_LIMIT_BACKEND=memory``, as Epic 67's authorize cache)."""

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.context import RequestContext, get_context
from app.core.developer import DeveloperContext
from app.core.errors import DomainError, Forbidden
from app.core.permissions import Principal
from app.core.settings import get_settings

ROLE_RANK = {"viewer": 0, "developer": 1, "admin": 2, "owner": 2}


def now() -> datetime:
    return datetime.now(UTC)


def platform_ctx(actor_id: uuid.UUID) -> RequestContext:
    """A platform-system context acting for a developer account (no tenant, no permissions; spec 0.1)."""
    base = get_context()
    return RequestContext(principal=Principal(type="platform", id=actor_id), ip=base.ip, user_agent=base.user_agent)


def require_org(dctx: DeveloperContext, org_id: uuid.UUID | None = None) -> uuid.UUID:
    if dctx.partner_org_id is None or (org_id is not None and dctx.partner_org_id != org_id):
        raise Forbidden("Select an organisation you belong to", code="partner_org_required")
    return dctx.partner_org_id


def require_role(dctx: DeveloperContext, minimum: str, org_id: uuid.UUID | None = None) -> uuid.UUID:
    org = require_org(dctx, org_id)
    if ROLE_RANK.get(str(dctx.role), -1) < ROLE_RANK[minimum]:
        raise Forbidden("Your role in this organisation does not allow this", code="org_role_required")
    return org


def unavailable() -> DomainError:
    exc = DomainError("The developer portal is temporarily unavailable", code="temporarily_unavailable")
    exc.status_code = 503
    return exc


# ================================================================ short-lived store
_memory: dict[str, tuple[str, float]] = {}


def _use_memory() -> bool:
    return get_settings().rate_limit_backend == "memory"


async def _redis() -> Any:
    from redis.asyncio import Redis

    s = get_settings()
    return Redis(host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, decode_responses=True,
                 socket_connect_timeout=0.5, socket_timeout=0.5)  # fmt: skip


async def kv_set(key: str, value: dict[str, Any], ttl: int) -> None:
    blob = json.dumps(value, default=str)
    if _use_memory():
        _memory[key] = (blob, time.monotonic() + ttl)
        return
    try:
        client = await _redis()
        await client.set(key, blob, ex=ttl)
        await client.aclose()
    except Exception as exc:
        raise unavailable() from exc


async def kv_get(key: str, *, pop: bool = False) -> dict[str, Any] | None:
    if _use_memory():
        hit = _memory.pop(key, None) if pop else _memory.get(key)
        if hit is None or hit[1] <= time.monotonic():
            _memory.pop(key, None)
            return None
        return dict(json.loads(hit[0]))
    try:
        client = await _redis()
        blob = await (client.getdel(key) if pop else client.get(key))
        await client.aclose()
    except Exception as exc:
        raise unavailable() from exc
    return dict(json.loads(blob)) if blob else None


async def kv_delete(key: str) -> None:
    if _use_memory():
        _memory.pop(key, None)
        return
    try:
        client = await _redis()
        await client.delete(key)
        await client.aclose()
    except Exception as exc:
        raise unavailable() from exc


__all__ = [
    "ROLE_RANK",
    "kv_delete",
    "kv_get",
    "kv_set",
    "now",
    "platform_ctx",
    "require_org",
    "require_role",
    "unavailable",
]
