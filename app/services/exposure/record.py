# ruff: file-ignore[line-too-long]
"""``subject_exposures`` writer (Epic 66, D-11, D-25(d)).

``record`` is called on hot paths (Public API responses, webhook deliveries, connector pushes, MCP results), so it only
buffers: a Redis hash per tenant (``exposure:buf:<tenant>``, field ``<installation>|<api key>|<type>|<id>|<channel>``,
value the latest ISO time) plus the set of dirty tenants, or an in-process buffer while Redis is unreachable (a 30 s
circuit breaker avoids paying the connect timeout per call). ``flush`` (beat ``exposure.flush``, every 60 s) upserts
the buffer per tenant: ``first_at`` = earliest, ``last_at`` = latest. Keys hold ids only, never PII.
"""

import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.ecosystem import EXPOSURE_CHANNELS, SubjectExposure

Channel = Literal["api", "webhook", "connector", "mcp"]
_TENANTS_KEY = "exposure:tenants"
_BREAKER_SECONDS = 30.0
_CONSTRAINT = "uq_subject_exposures_subject_channel"

_lock = threading.Lock()
# tenant -> field -> (first_at, last_at)
_memory: dict[str, dict[str, tuple[datetime, datetime]]] = {}
_redis_client = None
_redis_down_until = 0.0


def _redis() -> Any:  # redis client typing is loose
    """Return the lazy Redis client, or ``None`` while the breaker is open."""
    global _redis_client  # process-wide lazy client
    if time.monotonic() < _redis_down_until:
        return None
    if _redis_client is None:
        import redis

        from app.core.settings import get_settings

        s = get_settings()
        _redis_client = redis.Redis(
            host=s.redis_host, port=s.redis_port, password=s.redis_password or None, db=2,
            decode_responses=True, socket_connect_timeout=1, socket_timeout=2,
        )  # fmt: skip
    return _redis_client


def _trip(exc: Exception) -> None:
    global _redis_down_until  # process-wide breaker
    _redis_down_until = time.monotonic() + _BREAKER_SECONDS
    logger.warning("exposure buffer: Redis unavailable ({}); buffering in process", type(exc).__name__)


def _field(
    installation_id: uuid.UUID | None,
    api_key_id: uuid.UUID | None,
    subject_type: str,
    subject_id: uuid.UUID,
    channel: str,
) -> str:
    return f"{installation_id or ''}|{api_key_id or ''}|{subject_type}|{subject_id}|{channel}"


def _parse(field: str) -> tuple[uuid.UUID | None, uuid.UUID | None, str, uuid.UUID, str]:
    inst, key, stype, sid, channel = field.split("|")
    return (
        uuid.UUID(inst) if inst else None,
        uuid.UUID(key) if key else None,
        stype,
        uuid.UUID(sid),
        channel,
    )


def _memory_add(tenant: str, field: str, at: datetime) -> None:
    with _lock:
        bucket = _memory.setdefault(tenant, {})
        first, last = bucket.get(field, (at, at))
        bucket[field] = (min(first, at), max(last, at))


async def record(
    ctx: RequestContext,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    channel: Channel,
    installation_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
) -> None:
    """Buffer one exposure of ``(subject_type, subject_id)`` to an Installation OR an API key (exactly one)."""
    if (installation_id is None) == (api_key_id is None):
        msg = "exactly one of installation_id and api_key_id is required"
        raise ValueError(msg)
    if channel not in EXPOSURE_CHANNELS:
        msg = f"unknown exposure channel {channel!r}"
        raise ValueError(msg)
    if not subject_type or len(subject_type) > 40:
        msg = "subject_type must be 1..40 characters"
        raise ValueError(msg)
    if ctx.tenant_id is None:
        msg = "exposure.record needs a tenant context"
        raise ValueError(msg)
    tenant = str(ctx.tenant_id)
    field = _field(installation_id, api_key_id, subject_type, uuid.UUID(str(subject_id)), channel)
    at = datetime.now(UTC)
    client = _redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.hset(f"exposure:buf:{tenant}", field, at.isoformat())
            pipe.sadd(_TENANTS_KEY, tenant)
            pipe.execute()
            return
        except Exception as exc:
            _trip(exc)
    _memory_add(tenant, field, at)


def _drain_redis() -> dict[str, dict[str, tuple[datetime, datetime]]]:
    client = _redis()
    out: dict[str, dict[str, tuple[datetime, datetime]]] = {}
    if client is None:
        return out
    try:
        for tenant in client.smembers(_TENANTS_KEY):
            client.srem(_TENANTS_KEY, tenant)
            proc = f"exposure:proc:{tenant}:{uuid.uuid4().hex}"
            try:
                client.rename(f"exposure:buf:{tenant}", proc)
            except Exception:
                continue
            items = client.hgetall(proc)
            client.delete(proc)
            bucket = out.setdefault(tenant, {})
            for field, iso in items.items():
                at = datetime.fromisoformat(iso)
                bucket[field] = (at, at)
    except Exception as exc:
        _trip(exc)
    return out


def _drain_memory() -> dict[str, dict[str, tuple[datetime, datetime]]]:
    with _lock:
        out = dict(_memory)
        _memory.clear()
    return out


async def _upsert(
    db: AsyncSession, tenant_id: uuid.UUID, bucket: dict[str, tuple[datetime, datetime]]
) -> int:
    values = []
    for field, (first, last) in bucket.items():
        inst, key, stype, sid, channel = _parse(field)
        values.append(
            {"id": uuid.uuid4(), "tenant_id": tenant_id, "installation_id": inst, "api_key_id": key, "subject_type": stype,
             "subject_id": sid, "channel": channel, "first_at": first, "last_at": last}
        )  # fmt: skip
    total = 0
    for i in range(0, len(values), 500):
        stmt = pg_insert(SubjectExposure).values(values[i : i + 500])
        stmt = stmt.on_conflict_do_update(
            constraint=_CONSTRAINT,
            set_={
                "first_at": func.least(SubjectExposure.first_at, stmt.excluded.first_at),
                "last_at": func.greatest(SubjectExposure.last_at, stmt.excluded.last_at),
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)
        total += len(values[i : i + 500])
    return total


async def flush() -> int:
    """Upsert every buffered exposure (Redis and this process's fallback buffer). Returns the rows upserted.
    A tenant whose upsert fails keeps its entries (put back into the in-process buffer for the next flush)."""
    merged: dict[str, dict[str, tuple[datetime, datetime]]] = {}
    for source in (_drain_redis(), _drain_memory()):
        for tenant, bucket in source.items():
            target = merged.setdefault(tenant, {})
            for field, (first, last) in bucket.items():
                prev = target.get(field)
                target[field] = (min(prev[0], first), max(prev[1], last)) if prev else (first, last)
    total = 0
    for tenant, bucket in merged.items():
        try:
            async with tenant_session(uuid.UUID(tenant)) as db:
                total += await _upsert(db, uuid.UUID(tenant), bucket)
        except Exception:
            logger.exception("exposure flush failed for tenant {}; kept for the next flush", tenant)
            for field, (first, last) in bucket.items():
                _memory_add(tenant, field, first)
                _memory_add(tenant, field, last)
    return total


async def installations_exposed(
    db: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> list[uuid.UUID]:
    """Installations that received the subject through any channel (the D-11 ``subject.erased`` fan-out)."""
    rows = await db.execute(
        select(SubjectExposure.installation_id)
        .where(
            SubjectExposure.subject_type == subject_type,
            SubjectExposure.subject_id == subject_id,
            SubjectExposure.installation_id.is_not(None),
        )
        .distinct()
    )
    return sorted({r for r in rows.scalars() if r is not None}, key=str)


async def api_keys_exposed(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> list[uuid.UUID]:
    """Return the API keys that received the subject (D-25(d))."""
    rows = await db.execute(
        select(SubjectExposure.api_key_id)
        .where(
            SubjectExposure.subject_type == subject_type,
            SubjectExposure.subject_id == subject_id,
            SubjectExposure.api_key_id.is_not(None),
        )
        .distinct()
    )
    return sorted({r for r in rows.scalars() if r is not None}, key=str)


__all__ = ["Channel", "api_keys_exposed", "flush", "installations_exposed", "record"]
