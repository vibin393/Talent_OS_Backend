# ruff: noqa: E501, B008
"""Epic 66 (story 66.3, spec 0.7): Public API conventions -- cursors, ETag / If-Match, scopes, masking, deprecation,
OpenAPI separation and the API-key path (ecosystem gate)."""

import uuid
from datetime import date

import pytest
from fastapi import Depends, FastAPI, Request
from starlette.requests import Request as StarletteRequest

from app.core.errors import DomainError
from app.core.permissions import Principal, Scope
from app.core.public_api import (
    check_if_match,
    decode_cursor,
    deprecate,
    encode_cursor,
    etag_for,
    mask_for_scopes,
    public_openapi,
    public_router,
    require_scope,
    scope_satisfied,
)
from tests.epic3 import Org3
from tests.modules.identity.conftest import new_client
from tests.support.oauth import install_app, make_app, mint


def _req(method: str = "PATCH", headers: dict[str, str] | None = None) -> StarletteRequest:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return StarletteRequest(
        {"type": "http", "method": method, "headers": raw, "path": "/", "query_string": b""}
    )


def test_cursor_round_trip_and_tamper() -> None:
    rid = uuid.uuid4()
    cur = encode_cursor((date(2026, 10, 9), "x", 3), rid)
    assert decode_cursor(cur) == ((date(2026, 10, 9), "x", 3), rid)
    body, mac = cur.split(".")
    tampered = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + mac
    for bad in (tampered, "garbage", cur + "x"):
        with pytest.raises(DomainError) as exc:
            decode_cursor(bad)
        assert (exc.value.code, exc.value.status_code) == ("invalid_cursor", 400)


def test_etag_if_match() -> None:
    assert etag_for(3) == 'W/"3"'
    check_if_match(_req(headers={"If-Match": 'W/"3"'}), 3)
    check_if_match(_req(headers={"If-Match": '"3"'}), 3)
    with pytest.raises(DomainError) as exc:
        check_if_match(_req(headers={"If-Match": 'W/"2"'}), 3)
    assert exc.value.status_code == 412
    with pytest.raises(DomainError) as exc:
        check_if_match(_req(), 3)
    assert exc.value.status_code == 428
    check_if_match(_req("GET"), 3)


def _app_principal(scopes: set[str], permissions: dict[str, Scope] | None = None) -> Principal:
    return Principal(
        type="app",
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        permissions=permissions or {"candidate:read": Scope.TENANT},
        oauth_scopes=frozenset(scopes),
    )


def test_masking_by_pii_scopes() -> None:
    data = {
        "id": "1",
        "email": "a@example.test",
        "phone": "+91000",
        "pan": "X",
        "current_ctc": 10,
        "nested": [{"email": "b"}],
    }
    masked = mask_for_scopes(data, _app_principal({"candidates:read"}))
    assert masked == {
        "id": "1",
        "email": None,
        "phone": None,
        "pan": None,
        "current_ctc": None,
        "nested": [{"email": None}],
    }
    contact = mask_for_scopes(data, _app_principal({"candidates:read", "pii.contact:read"}))
    assert contact["email"] == "a@example.test" and contact["pan"] is None and contact["current_ctc"] is None
    api_key = Principal(type="api_key", id=uuid.uuid4(), permissions={"candidate:read": Scope.TENANT})
    assert mask_for_scopes(data, api_key)["email"] is None


def test_scope_rules_for_api_keys() -> None:
    key = Principal(type="api_key", id=uuid.uuid4(), permissions={"candidate:read": Scope.TENANT})
    assert scope_satisfied(key, "candidates:read")
    assert not scope_satisfied(key, "candidates:write")
    assert not scope_satisfied(key, "pii.contact:read")  # FR-348: sensitive scopes never for API keys


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    router = public_router("probe")

    @router.get(
        "/api/public/v1/__probe/candidates",
        dependencies=[
            Depends(deprecate(since=date(2026, 1, 1), sunset=date(2027, 1, 1), link="https://example.test/d"))
        ],
    )
    async def probe(
        request: Request, principal: Principal = Depends(require_scope("candidates:read"))
    ) -> dict[str, str]:
        return {"type": principal.type, "deprecated": str(getattr(request.state, "tos_deprecated", False))}

    @router.get("/api/public/v1/__probe/write")
    async def probe_write(
        principal: Principal = Depends(require_scope("candidates:write")),
    ) -> dict[str, str]:
        return {"type": principal.type}

    app.include_router(router)
    return app


async def test_require_scope_and_deprecation_headers(probe_app: FastAPI, org3: Org3) -> None:
    app_row = await make_app(scopes=("candidates:read",))
    inst = await install_app(org3.tenant_id, app_row, granted_by=org3.admin.user_id)
    token = (await mint(inst, scopes=("candidates:read",))).access_token
    auth = {"Authorization": f"Bearer {token}"}
    async with new_client(probe_app) as client:
        ok = await client.get("/api/public/v1/__probe/candidates", headers=auth)
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"type": "app", "deprecated": "True"}
        assert ok.headers["Sunset"].startswith("Fri, 01 Jan 2027") and "deprecation" in ok.headers["Link"]
        denied = await client.get("/api/public/v1/__probe/write", headers=auth)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "insufficient_scope"
        assert denied.json()["error"]["details"]["required_scopes"] == ["candidates:write"]
        # caller-supplied tenant / scope parameters are ignored
        spoof = await client.get(
            "/api/public/v1/__probe/write?tenant=other&scope=candidates:write",
            headers={**auth, "X-Tenant": "x"},
        )
        assert spoof.status_code == 403


def test_openapi_documents_only_public_routes(app: FastAPI) -> None:
    doc = public_openapi(app)
    paths = set(doc["paths"])
    assert "/api/public/v1/meta" in paths
    assert all(p.startswith("/api/public/v1") for p in paths)
    internal = app.openapi()
    assert not any(p.startswith("/api/public/v1") for p in internal["paths"])


async def test_no_bearer_is_401(app: FastAPI) -> None:
    async with new_client(app) as client:
        resp = await client.get("/api/public/v1/meta")
    assert resp.status_code == 401
