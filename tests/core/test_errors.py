"""AD-12 error envelope for domain errors, validation errors, unknown routes and unhandled DB errors."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.core import errors
from app.core.database import tenant_session
from app.main import create_app


class ItemIn(BaseModel):
    name: str
    qty: int


ERROR_CASES = [
    (errors.NotFound, 404, "not_found"),
    (errors.Forbidden, 403, "forbidden"),
    (errors.Unauthorized, 401, "unauthorized"),
    (errors.ConflictError, 409, "conflict"),
    (errors.ValidationFailed, 422, "validation_failed"),
    (errors.RateLimited, 429, "rate_limited"),
]


@pytest.fixture(scope="module")
def error_app(rls_table: str) -> FastAPI:
    app = create_app()

    @app.get("/api/v1/_test/raise/{idx}")
    async def raise_domain(idx: int) -> None:
        exc_cls = ERROR_CASES[idx][0]
        raise exc_cls("Widget went wrong", details={"widget_id": "w1"})

    @app.post("/api/v1/_test/items")
    async def create_item(item: ItemIn) -> ItemIn:
        return item

    @app.get("/api/v1/_test/http-detail")
    async def http_detail() -> None:
        raise HTTPException(status_code=400, detail={"reason": "bad"})

    @app.get("/api/v1/_test/cross-tenant-write")
    async def cross_tenant_write() -> None:
        async with tenant_session(uuid.uuid4()) as s:
            await s.execute(
                text(f"INSERT INTO {rls_table} (tenant_id, name) VALUES (:t, 'x')"), {"t": uuid.uuid4()}
            )

    return app


@pytest.fixture
async def eclient(error_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=error_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.mark.parametrize(("idx", "status", "code"), [(i, c[1], c[2]) for i, c in enumerate(ERROR_CASES)])
async def test_domain_errors_render_envelope(
    eclient: httpx.AsyncClient, idx: int, status: int, code: str
) -> None:
    response = await eclient.get(f"/api/v1/_test/raise/{idx}")
    assert response.status_code == status
    # Epic 14 (FR-82): a 403 also tells who can grant access (``ask``).
    details = {"widget_id": "w1", **({"ask": "tenant_admin"} if status == 403 else {})}
    assert response.json() == {"error": {"code": code, "message": "Widget went wrong", "details": details}}


async def test_default_message_and_details(eclient: httpx.AsyncClient) -> None:
    exc = errors.NotFound()
    assert (exc.status_code, exc.code, exc.message) == (404, "not_found", "Resource not found")
    assert exc.details == {}


async def test_request_validation_error_envelope(eclient: httpx.AsyncClient) -> None:
    response = await eclient.post("/api/v1/_test/items", json={"qty": "not-a-number"})
    assert response.status_code == 422
    err = response.json()["error"]
    assert err["code"] == "validation_failed"
    fields = {f["field"]: f for f in err["details"]["fields"]}
    assert set(fields) == {"body.name", "body.qty"}
    assert fields["body.name"]["type"] == "missing"


async def test_unknown_route_uses_envelope(eclient: httpx.AsyncClient) -> None:
    response = await eclient.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_rls_write_violation_surfaces_as_500(eclient: httpx.AsyncClient) -> None:
    response = await eclient.get("/api/v1/_test/cross-tenant-write", headers={"X-Request-ID": "req-500"})
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "req-500"
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred", "details": {}}
    }


async def test_http_exception_non_string_detail_is_kept(eclient: httpx.AsyncClient) -> None:
    response = await eclient.get("/api/v1/_test/http-detail")
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"detail": {"reason": "bad"}}
