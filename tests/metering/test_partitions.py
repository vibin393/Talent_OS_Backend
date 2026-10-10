# ruff: noqa: E501
"""``api_request_log`` monthly partitions: RLS on the parent and on every partition, ``ensure_monthly`` idempotent,
retention purge."""

import datetime as dt
import uuid

from sqlalchemy import text

from app.core.database import tenant_session
from app.core.migration_helpers import monthly_partition_name
from app.services import metering_service
from app.services.metering import partitions
from tests.factories import create_tenant_row


async def _insert(tenant: uuid.UUID, at: dt.datetime) -> uuid.UUID:
    rid = uuid.uuid4()
    async with tenant_session(tenant) as db:
        await db.execute(
            text(
                "INSERT INTO api_request_log (id, tenant_id, at, api_key_id, api_version) "
                "VALUES (:id, :t, :at, :k, 'v1')"
            ),
            {"id": rid, "t": tenant, "at": at, "k": uuid.uuid4()},
        )
    return rid


async def _count(tenant: uuid.UUID, table: str, rid: uuid.UUID) -> int:
    async with tenant_session(tenant) as db:
        return int(
            (await db.execute(text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": rid})).scalar_one()
        )


async def test_partition_rls() -> None:
    a, b = await create_tenant_row("Part A"), await create_tenant_row("Part B")
    now = dt.datetime.now(dt.UTC)
    rid = await _insert(a, now)
    part = monthly_partition_name("api_request_log", now.year, now.month)
    assert await _count(a, "api_request_log", rid) == 1
    assert await _count(a, part, rid) == 1
    assert await _count(b, "api_request_log", rid) == 0
    assert await _count(b, part, rid) == 0


async def test_ensure_monthly_idempotent() -> None:
    await metering_service.ensure_monthly()
    again = await metering_service.ensure_monthly()
    assert again["api_request_log"] == []
    far = dt.date(2031, 6, 15)
    name = monthly_partition_name("api_request_log", 2031, 6)
    first = await metering_service.ensure_monthly(0, today=far)
    assert first["api_request_log"] in ([name], [])  # [] when an earlier run on this scratch DB created it
    assert (await metering_service.ensure_monthly(0, today=far))["api_request_log"] == []
    assert name in await partitions.existing_partitions("api_request_log")


def test_registry_is_idempotent() -> None:
    metering_service.register_partitioned_table("api_request_log", retention_days=30)
    names = [t.name for t in metering_service.registered_tables()]
    assert names.count("api_request_log") == 1


async def test_purge_drops_expired_months_and_rows() -> None:
    tenant = await create_tenant_row("Purge")
    now = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    await metering_service.ensure_monthly(1, today=dt.date(2026, 8, 1))  # August + September
    aug = await _insert(tenant, dt.datetime(2026, 8, 30, tzinfo=dt.UTC))
    old_sep = await _insert(tenant, dt.datetime(2026, 9, 5, tzinfo=dt.UTC))
    new_sep = await _insert(tenant, dt.datetime(2026, 9, 20, tzinfo=dt.UTC))
    out = await metering_service.purge(now=now)
    assert out["api_request_log"]["partitions_dropped"] >= 1
    existing = await partitions.existing_partitions("api_request_log")
    assert monthly_partition_name("api_request_log", 2026, 8) not in existing
    assert await _count(tenant, "api_request_log", aug) == 0
    assert await _count(tenant, "api_request_log", old_sep) == 0
    assert await _count(tenant, "api_request_log", new_sep) == 1
