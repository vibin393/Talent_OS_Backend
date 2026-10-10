# ruff: noqa: E501
"""``metering.flush`` (beat every 60 s, queue ``maintenance``): Redis hashes + the in-process buffer ->
``usage_counters`` (``ON CONFLICT ON CONSTRAINT uq_usage_counters_key`` increments), one tenant session per tenant.

Redis keys are claimed atomically (``RENAME`` to a ``:flushing:`` key) so concurrent ``HINCRBY``s land in a fresh hash;
when the upsert fails the claimed values are put back (Redis, else the buffer), so nothing is counted twice or lost.
"""

import datetime as dt
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import text

from app.core.database import tenant_session
from app.services.metering import record as rec

_UPSERT = text(
    "INSERT INTO usage_counters (id, tenant_id, day, unit, installation_id, api_key_id, app_id, route_group, "
    "count, error_count, quantity, created_at, updated_at) "
    "VALUES (:id, CAST(:tenant_id AS uuid), :day, :unit, CAST(:installation_id AS uuid), CAST(:api_key_id AS uuid), "
    "CAST(:app_id AS uuid), :route_group, :count, :error_count, :quantity, now(), now()) "
    "ON CONFLICT ON CONSTRAINT uq_usage_counters_key DO UPDATE SET "
    "count = usage_counters.count + EXCLUDED.count, "
    "error_count = usage_counters.error_count + EXCLUDED.error_count, "
    "quantity = usage_counters.quantity + EXCLUDED.quantity, "
    "app_id = COALESCE(usage_counters.app_id, EXCLUDED.app_id), "
    "updated_at = now()"
)


def _merge(into: dict[rec.BufferKey, list[Any]], rows: dict[rec.BufferKey, list[Any]]) -> None:
    for key, (c, e, q) in rows.items():
        row = into.setdefault(key, [0, 0, Decimal(0)])
        row[0] += c
        row[1] += e
        row[2] += q


async def _claim_redis() -> tuple[dict[rec.BufferKey, list[Any]], list[str]]:
    """Claim every ``usage:*`` hash (not already being flushed). Returns the folded rows and the claimed keys."""
    rows: dict[rec.BufferKey, list[Any]] = {}
    claimed: list[str] = []
    client = rec.redis_client()
    keys = [k async for k in client.scan_iter(match=f"{rec.KEY_PREFIX}*", count=500)]
    for key in keys:
        parts = key.split(":")
        if len(parts) == 3:  # live key usage:{tenant}:{day}
            tmp = f"{key}:flushing:{uuid.uuid4().hex}"
            try:
                await client.rename(key, tmp)
            except Exception:  # vanished between SCAN and RENAME
                continue
        elif len(parts) == 5 and parts[3] == "flushing":  # left over by a crashed flush
            tmp = key
        else:
            continue
        values = await client.hgetall(tmp)
        _merge(rows, rec.fold_hash(parts[1], parts[2], values.items()))
        claimed.append(tmp)
    return rows, claimed


async def _put_back(rows: dict[rec.BufferKey, list[Any]]) -> None:
    for key, (c, e, q) in rows.items():
        if rec.use_memory():
            rec.buffer_add(key, c, e, q)
        else:
            await rec._push(key, c, e, q)  # falls back to the buffer itself


async def _upsert_tenant(tenant: str, rows: dict[rec.BufferKey, list[Any]]) -> int:
    async with tenant_session(uuid.UUID(tenant)) as db:
        for (_, day, unit, installation, api_key, app, route_group), (c, e, q) in rows.items():
            await db.execute(
                _UPSERT,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant,
                    "day": dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC).date(),
                    "unit": unit,
                    "installation_id": installation or None,
                    "api_key_id": api_key or None,
                    "app_id": app or None,
                    "route_group": route_group,
                    "count": int(c),
                    "error_count": int(e),
                    "quantity": Decimal(q),
                },
            )
    return len(rows)


async def flush() -> dict[str, int]:
    """Drain Redis and the buffer into ``usage_counters``. Returns rows upserted per tenant (failed tenants: -1)."""
    await rec.drain_pending()
    rows = rec.buffer_drain()
    claimed: list[str] = []
    if not rec.use_memory():
        try:
            redis_rows, claimed = await _claim_redis()
            _merge(rows, redis_rows)
        except Exception as exc:
            logger.warning(
                "Metering flush: Redis unavailable ({}); flushing the buffer only", type(exc).__name__
            )
    by_tenant: dict[str, dict[rec.BufferKey, list[Any]]] = defaultdict(dict)
    for key, value in rows.items():
        if value[0] or value[1] or value[2]:
            by_tenant[key[0]][key] = value
    result: dict[str, int] = {}
    for tenant, tenant_rows in by_tenant.items():
        try:
            result[tenant] = await _upsert_tenant(tenant, tenant_rows)
        except Exception:
            logger.exception("Metering flush failed for tenant {}; usage kept for the next run", tenant)
            await _put_back(tenant_rows)
            result[tenant] = -1
    if claimed:
        try:
            await rec.redis_client().delete(*claimed)
        except Exception:
            logger.exception("Metering flush: could not delete claimed keys")
    return result


__all__ = ["flush"]
