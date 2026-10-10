"""Hot path ``record`` (never raises; buffer fallback) and ``flush`` (idempotent upsert sums)."""

import datetime as dt
import statistics
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.metering import UsageCounter
from app.schemas.metering import UsageQuery
from app.services import metering_service
from app.services.metering import record as rec


async def _counters(tenant: uuid.UUID) -> list[UsageCounter]:
    async with tenant_session(tenant) as db:
        return list((await db.execute(select(UsageCounter).order_by(UsageCounter.unit))).scalars())


async def test_record_never_raises(tenant: uuid.UUID, clean_buffer: None) -> None:
    await metering_service.record(RequestContext(tenant_id=None), "api_request")
    await metering_service.record(RequestContext(tenant_id=tenant), "not_a_unit")
    await metering_service.record(object(), "api_request")
    await metering_service.record(RequestContext(tenant_id=tenant), "api_request", quantity="x")  # type: ignore[arg-type]
    assert rec.buffer_size() == 0


async def test_redis_down_falls_back_to_buffer(
    tenant: uuid.UUID, clean_buffer: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_backend", "redis")

    def boom() -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(rec, "redis_client", boom)
    ctx = RequestContext(tenant_id=tenant)
    await metering_service.record(ctx, "api_request", installation_id=uuid.uuid4(), error=True)
    await rec.drain_pending()
    drained = rec.buffer_drain()
    assert len(drained) == 1
    assert next(iter(drained.values()))[:2] == [1, 1]


async def test_buffer_is_bounded(
    tenant: uuid.UUID, clean_buffer: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rec, "BUFFER_LIMIT", 3)
    ctx = RequestContext(tenant_id=tenant)
    for i in range(5):
        await metering_service.record(ctx, "api_request", route_group=f"g{i}")
    await metering_service.record(ctx, "api_request", route_group="g0")  # existing entries still count
    drained = rec.buffer_drain()
    assert len(drained) == 3
    assert sum(v[0] for v in drained.values()) == 4


async def test_flush_upserts_idempotent_sums(tenant: uuid.UUID, clean_buffer: None) -> None:
    ctx = RequestContext(tenant_id=tenant)
    inst, key = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await metering_service.record(ctx, "api_request", installation_id=inst, route_group="candidates")
    await metering_service.record(
        ctx, "api_request", installation_id=inst, route_group="candidates", error=True
    )
    await metering_service.record(ctx, "api_request", api_key_id=key)
    await metering_service.record(ctx, "telephony_minute", Decimal("2.5"), inst)
    result = await metering_service.flush()
    assert result[str(tenant)] == 3
    assert await metering_service.flush() == {}  # nothing new: no double counting
    await metering_service.record(ctx, "api_request", installation_id=inst, route_group="candidates")
    await metering_service.flush()
    rows = await _counters(tenant)
    by = {(r.unit, r.installation_id, r.api_key_id): r for r in rows}
    inst_row = by["api_request", inst, None]
    assert (inst_row.count, inst_row.error_count, inst_row.route_group) == (5, 1, "candidates")
    assert inst_row.day == dt.datetime.now(dt.UTC).date()
    assert by["api_request", None, key].count == 1
    minutes = by["telephony_minute", inst, None]
    assert minutes.quantity == Decimal("2.500") and minutes.count == 1


async def test_usage_read(tenant: uuid.UUID, clean_buffer: None) -> None:
    ctx = RequestContext(tenant_id=tenant)
    inst = uuid.uuid4()
    for _ in range(2):
        await metering_service.record(ctx, "api_request", installation_id=inst)
    await metering_service.record(ctx, "webhook_attempt", installation_id=inst, error=True)
    await metering_service.flush()
    today = dt.datetime.now(dt.UTC).date()
    q = UsageQuery(date_from=today, date_to=today, group_by=["unit", "installation"])
    async with tenant_session(tenant) as db:
        out = await metering_service.usage(ctx, db, q)
    rows = {(r.unit, r.installation_id): r for r in out.rows}
    assert rows["api_request", inst].count == 2
    assert rows["webhook_attempt", inst].error_count == 1
    assert {t.unit for t in out.totals} == {"api_request", "webhook_attempt"}


async def test_platform_usage_by_app(clean_buffer: None) -> None:
    from app.schemas.metering import PlatformUsageQuery
    from tests.factories import create_tenant_row

    app_id = uuid.uuid4()
    for name in ("PU A", "PU B"):
        tid = await create_tenant_row(name)
        ctx = RequestContext(tenant_id=tid)
        await metering_service.record(ctx, "api_request", installation_id=uuid.uuid4(), app_id=app_id)
    await metering_service.flush()
    today = dt.datetime.now(dt.UTC).date()
    out = await metering_service.platform_usage(
        PlatformUsageQuery(date_from=today, date_to=today, app_id=app_id)
    )
    assert [(r.app_id, r.unit, r.count, r.tenant_count) for r in out.rows] == [(app_id, "api_request", 2, 2)]


@pytest.mark.slow
async def test_record_p95_budget(tenant: uuid.UUID, clean_buffer: None) -> None:
    """NFR-71: ``record`` <= 5 ms p95 (in-process buffer)."""
    ctx = RequestContext(tenant_id=tenant)
    inst = uuid.uuid4()
    samples = []
    for i in range(2000):
        t0 = time.perf_counter()
        await metering_service.record(ctx, "api_request", installation_id=inst, route_group=f"g{i % 20}")
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 <= 5.0, p95
