import asyncio
import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.database import platform_session, tenant_session
from app.core.sequences import next_human_id
from tests.factories import create_tenant_row


async def _next(tenant: uuid.UUID, key: str, prefix: str | None = None, width: int = 4) -> str:
    async with tenant_session(tenant) as s:
        return await next_human_id(s, key, prefix, width)


async def test_sequential_values_per_tenant_and_key() -> None:
    tenant, other = await create_tenant_row(), await create_tenant_row()
    assert await _next(tenant, "REQ") == "REQ-0001"
    assert await _next(tenant, "REQ") == "REQ-0002"
    assert await _next(tenant, "CAN", width=6) == "CAN-000001"
    assert await _next(other, "REQ") == "REQ-0001"
    assert await _next(tenant, "demand", prefix="D") == "D-0001"


async def test_concurrent_calls_get_distinct_sequential_values() -> None:
    tenant = await create_tenant_row()

    async def one() -> str:
        async with tenant_session(tenant) as s:
            value = await next_human_id(s, "REQ")
            await asyncio.sleep(0.01)  # hold the row lock a little to force contention
            return value

    results = await asyncio.gather(*(one() for _ in range(8)))
    assert sorted(results) == [f"REQ-{i:04d}" for i in range(1, 9)]


async def test_requires_tenant_context() -> None:
    with pytest.raises(DBAPIError):
        async with platform_session() as s:
            await next_human_id(s, "REQ")
