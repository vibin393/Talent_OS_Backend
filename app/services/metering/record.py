# ruff: noqa: E501
"""Metering hot path (Epic 66, AD-78, NFR-71): ``record`` never raises and never waits on a database.

Redis: one pipeline of ``HINCRBY`` / ``HINCRBYFLOAT`` on ``usage:{tenant}:{yyyymmdd}`` (fields
``{unit}|{installation}|{api_key}|{app}|{route_group}|{c|e|q}``), sent fire-and-forget from a background task.
``RATE_LIMIT_BACKEND=memory`` (tests) or Redis down -> the in-process buffer (bounded: beyond ``BUFFER_LIMIT``
distinct entries new ones are dropped with a throttled warning). ``metering.flush`` drains both.
"""

import asyncio
import datetime as dt
import threading
import time
import uuid
import weakref
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from loguru import logger

from app.core.settings import get_settings
from app.models.metering import USAGE_UNITS

KEY_PREFIX = "usage:"
KEY_TTL_SECONDS = 3 * 86_400
BUFFER_LIMIT = 10_000
SEP = "|"

# (tenant, day, unit, installation, api_key, app, route_group) -> [count, error_count, quantity]
BufferKey = tuple[str, str, str, str, str, str, str]
_buffer: dict[BufferKey, list[Any]] = {}
_lock = threading.Lock()
_pending: set[asyncio.Task[None]] = set()
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()
_warned = {"drop": 0.0, "redis": 0.0}
_force_memory = {"on": False}


def _u(value: uuid.UUID | str | None) -> str:
    return str(value) if value else ""


def day_key(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def redis_key(tenant_id: uuid.UUID | str, day: dt.date) -> str:
    return f"{KEY_PREFIX}{tenant_id}:{day_key(day)}"


def field_base(unit: str, installation: str, api_key: str, app: str, route_group: str) -> str:
    return SEP.join((unit, installation, api_key, app, route_group))


def use_memory() -> bool:
    return _force_memory["on"] or get_settings().rate_limit_backend == "memory"


def set_force_memory(on: bool) -> None:
    """Test / ops hook: keep every record in the in-process buffer."""
    _force_memory["on"] = on


def _throttled_warning(kind: str, message: str, *args: object) -> None:
    now = time.monotonic()
    if now - _warned[kind] > 60:
        _warned[kind] = now
        logger.warning(message, *args)


def buffer_add(key: BufferKey, count: int, errors: int, quantity: Decimal) -> bool:
    with _lock:
        row = _buffer.get(key)
        if row is None:
            if len(_buffer) >= BUFFER_LIMIT:
                _throttled_warning("drop", "Metering buffer full ({} entries); dropping usage", BUFFER_LIMIT)
                return False
            row = _buffer[key] = [0, 0, Decimal(0)]
        row[0] += count
        row[1] += errors
        row[2] += quantity
    return True


def buffer_drain() -> dict[BufferKey, list[Any]]:
    global _buffer  # swapped atomically under the lock
    with _lock:
        out, _buffer = _buffer, {}
    return out


def buffer_size() -> int:
    return len(_buffer)


def redis_client() -> Any:
    """One ``redis.asyncio`` client per event loop (db 2, short timeouts; shared with the rate limiter's db)."""
    from redis.asyncio import Redis

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        s = get_settings()
        client = Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=2,
            password=s.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        _clients[loop] = client
    return client


async def _push(key: BufferKey, count: int, errors: int, quantity: Decimal) -> None:
    tenant, day, *rest = key
    base = field_base(*rest)
    rkey = f"{KEY_PREFIX}{tenant}:{day}"
    try:
        pipe = redis_client().pipeline(transaction=False)
        pipe.hincrby(rkey, base + SEP + "c", count)
        if errors:
            pipe.hincrby(rkey, base + SEP + "e", errors)
        if quantity:
            pipe.hincrbyfloat(rkey, base + SEP + "q", float(quantity))
        pipe.expire(rkey, KEY_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:
        _throttled_warning(
            "redis", "Metering: Redis unavailable ({}); buffering in process", type(exc).__name__
        )
        buffer_add(key, count, errors, quantity)


async def record(
    ctx: Any,
    unit: str,
    quantity: Decimal | int = 1,
    installation_id: uuid.UUID | None = None,
    *,
    api_key_id: uuid.UUID | None = None,
    route_group: str = "",
    error: bool = False,
    app_id: uuid.UUID | None = None,
) -> None:
    """Count one ``unit`` for ``ctx.tenant_id`` (today, UTC). ``quantity`` is the measured amount (minutes, tokens)
    for quantity units; for count units it stays 1. Fire-and-forget: never raises, never blocks on I/O."""
    try:
        tenant_id = getattr(ctx, "tenant_id", None)
        if tenant_id is None:
            logger.warning("Metering: no tenant on the context; {} not recorded", unit)
            return
        if unit not in USAGE_UNITS:
            logger.warning("Metering: unknown unit {!r}; not recorded", unit)
            return
        qty = Decimal(str(quantity)) if not isinstance(quantity, Decimal) else quantity
        key: BufferKey = (
            str(tenant_id),
            day_key(dt.datetime.now(dt.UTC).date()),
            unit,
            _u(installation_id),
            _u(api_key_id),
            _u(app_id),
            (route_group or "")[:60],
        )
        errors = 1 if error else 0
        quantity_value = qty if unit.endswith("_minute") or qty != 1 else Decimal(0)
        if use_memory():
            buffer_add(key, 1, errors, quantity_value)
            return
        try:
            task = asyncio.get_running_loop().create_task(_push(key, 1, errors, quantity_value))
        except RuntimeError:
            buffer_add(key, 1, errors, quantity_value)
            return
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:
        logger.exception("Metering: record failed for {}", unit)


async def drain_pending() -> None:
    """Wait for in-flight Redis pushes of this loop (flush, tests)."""
    loop = asyncio.get_running_loop()
    mine = [t for t in list(_pending) if t.get_loop() is loop]
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)


def parse_field(field: str) -> tuple[str, str, str, str, str, str] | None:
    parts = field.split(SEP)
    if len(parts) != 6:
        return None
    unit, installation, api_key, app, route_group, kind = parts
    return unit, installation, api_key, app, route_group, kind


def fold_hash(tenant: str, day: str, values: Iterable[tuple[str, str]]) -> dict[BufferKey, list[Any]]:
    """Redis hash fields -> buffer rows (``[count, error_count, quantity]``)."""
    out: dict[BufferKey, list[Any]] = {}
    for field, raw in values:
        parsed = parse_field(field)
        if parsed is None:
            continue
        unit, installation, api_key, app, route_group, kind = parsed
        row = out.setdefault((tenant, day, unit, installation, api_key, app, route_group), [0, 0, Decimal(0)])
        if kind == "c":
            row[0] += int(float(raw))
        elif kind == "e":
            row[1] += int(float(raw))
        elif kind == "q":
            row[2] += Decimal(str(raw))
    return out


__all__ = [
    "BUFFER_LIMIT",
    "KEY_PREFIX",
    "buffer_add",
    "buffer_drain",
    "buffer_size",
    "day_key",
    "drain_pending",
    "fold_hash",
    "record",
    "redis_client",
    "redis_key",
    "set_force_memory",
    "use_memory",
]
