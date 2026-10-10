# ruff: noqa: E501, D401
"""Per-principal portal Copilot question limit (Epic 77 A4): a Redis fixed window
``pcopilot:rl:<tenant>:<principal_type>:<principal_id>:<yyyymmddhh>`` (INCR + EXPIRE 3,600). 30 / hour for Client
and Vendor Users, 15 / hour for Candidates by default. Fails open when Redis is down (as P4); the AI budget and the
run caps still apply."""

import uuid
from datetime import UTC, datetime

from loguru import logger

from app.services.copilot.ratelimit import WINDOW_SECONDS, Window
from app.services.copilot.streaming import redis_client


def key(tenant_id: uuid.UUID, principal_type: str, principal_id: uuid.UUID, now: datetime) -> str:
    return f"pcopilot:rl:{tenant_id}:{principal_type}:{principal_id}:{now:%Y%m%d%H}"


def _retry_after(now: datetime) -> int:
    return max(1, WINDOW_SECONDS - (now.minute * 60 + now.second))


async def hit(
    tenant_id: uuid.UUID,
    principal_type: str,
    principal_id: uuid.UUID,
    limit: int,
    *,
    now: datetime | None = None,
) -> Window:
    now = now or datetime.now(UTC)
    client = redis_client()
    try:
        k = key(tenant_id, principal_type, principal_id, now)
        used = int(await client.incr(k))
        if used == 1:
            await client.expire(k, WINDOW_SECONDS)
    except Exception:
        logger.warning("Portal Copilot rate limit unavailable (Redis); allowing the question")
        return Window(True, 0, limit, 0)
    finally:
        await client.aclose()
    return Window(used <= limit, used, max(0, limit - used), _retry_after(now))


async def remaining(
    tenant_id: uuid.UUID,
    principal_type: str,
    principal_id: uuid.UUID,
    limit: int,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    client = redis_client()
    try:
        raw = await client.get(key(tenant_id, principal_type, principal_id, now))
    except Exception:
        return limit
    finally:
        await client.aclose()
    return max(0, limit - int(raw or 0))


async def reset(tenant_id: uuid.UUID, principal_type: str, principal_id: uuid.UUID) -> None:
    """Tests / support only."""
    client = redis_client()
    try:
        await client.delete(key(tenant_id, principal_type, principal_id, datetime.now(UTC)))
    finally:
        await client.aclose()


__all__ = ["hit", "key", "remaining", "reset"]
