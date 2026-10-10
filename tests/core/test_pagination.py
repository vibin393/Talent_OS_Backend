import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.pagination import MAX_PAGE, Page, PageParams, paginate
from app.core.sequences import TenantSequence, next_value
from app.main import create_app
from tests.factories import create_tenant_row


async def test_paginate_returns_envelope_for_one_page() -> None:
    tenant = await create_tenant_row()
    async with tenant_session(tenant) as s:
        for i in range(25):
            await next_value(s, f"K{i:02d}")
    async with tenant_session(tenant) as s:
        page = await paginate(s, select(TenantSequence).order_by(TenantSequence.key), PageParams(3, 10))
    assert (page.total, page.page, page.page_size) == (25, 3, 10)
    assert [row.key for row in page.items] == [f"K{i:02d}" for i in range(20, 25)]


@pytest.fixture(scope="module")
def page_app() -> FastAPI:
    app = create_app()

    @app.get("/api/v1/_test/page")
    async def page(params: Annotated[PageParams, Depends()]) -> Page[int]:
        return Page[int](items=[], total=0, page=params.page, page_size=params.page_size)

    return app


@pytest.fixture
async def pclient(page_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=page_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_page_params_defaults_and_limits(pclient: httpx.AsyncClient) -> None:
    ok = await pclient.get("/api/v1/_test/page")
    assert ok.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert (await pclient.get("/api/v1/_test/page?page_size=100")).status_code == 200
    too_big = await pclient.get("/api/v1/_test/page?page_size=101")
    assert too_big.status_code == 422
    assert too_big.json()["error"]["details"]["fields"][0]["field"] == "query.page_size"
    assert (await pclient.get("/api/v1/_test/page?page=0")).status_code == 422
    assert (await pclient.get(f"/api/v1/_test/page?page={MAX_PAGE}&page_size=100")).status_code == 200
    assert (await pclient.get(f"/api/v1/_test/page?page={MAX_PAGE + 1}")).status_code == 422


async def test_paginate_requires_order_by() -> None:
    async with tenant_session(uuid.uuid4()) as s:
        with pytest.raises(ValueError, match="ORDER BY"):
            await paginate(s, select(TenantSequence), PageParams())
