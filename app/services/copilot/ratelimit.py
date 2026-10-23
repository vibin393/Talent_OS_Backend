# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Per-user Copilot question rate limit (Epic 54, FR-280): a Redis fixed window
``copilot:rl:<tenant>:<user>:<yyyymmddhh>`` (INCR + EXPIRE 3,600). Fails open when Redis is unavailable (the runtime's
token budget and caps still apply)."""

import dataclasses
import uuid
from datetime import UTC, datetime

from loguru import logger

from app.services.copilot.streaming import redis_client

WINDOW_SECONDS = 3600


@dataclasses.dataclass(frozen=True, slots=True)
class Window:
    allowed: bool
    used: int
    remaining: int
    retry_after_s: int


def _key(tenant_id: uuid.UUID, user_id: uuid.UUID, now: datetime) -> str:
    return f"copilot:rl:{tenant_id}:{user_id}:{now:%Y%m%d%H}"


def _retry_after(now: datetime) -> int:
    return max(1, WINDOW_SECONDS - (now.minute * 60 + now.second))


async def hit(tenant_id: uuid.UUID, user_id: uuid.UUID, limit: int, *, now: datetime | None = None) -> Window:
    """Count one question; ``allowed`` is False once the window holds more than ``limit``."""
    now = now or datetime.now(UTC)
    client = redis_client()
    try:
        key = _key(tenant_id, user_id, now)
        used = int(await client.incr(key))
        if used == 1:
            await client.expire(key, WINDOW_SECONDS)
    except Exception:
        logger.warning("Copilot rate limit unavailable (Redis); allowing the question")
        return Window(True, 0, limit, 0)
    finally:
        await client.aclose()
    return Window(used <= limit, used, max(0, limit - used), _retry_after(now))


async def remaining(
    tenant_id: uuid.UUID, user_id: uuid.UUID, limit: int, *, now: datetime | None = None
) -> int:
    now = now or datetime.now(UTC)
    client = redis_client()
    try:
        raw = await client.get(_key(tenant_id, user_id, now))
    except Exception:
        return limit
    finally:
        await client.aclose()
    return max(0, limit - int(raw or 0))


async def reset(tenant_id: uuid.UUID, user_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Tests / support only."""
    client = redis_client()
    try:
        await client.delete(_key(tenant_id, user_id, now or datetime.now(UTC)))
    finally:
        await client.aclose()


__all__ = ["WINDOW_SECONDS", "Window", "hit", "remaining", "reset"]
