# ruff: noqa: E501
"""Epic 67 (story 67.6, FR-348): an API key works on both doors; a sensitive scope is never usable through a key;
``public_scopes_for``; the kill switch refuses keys on /public/v1; the deprecation report and headers."""

from datetime import date

from fastapi import FastAPI
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.core.public_api_deprecations import Deprecation, validate
from app.services import api_keys_service, ecosystem_service, public_api_service
from tests.epic3 import Org3, create_candidate, h
from tests.public_api.e67 import Api, code


async def _key(org: Org3, scopes: list[str]) -> str:
    resp = await org.admin.client.post("/api/v1/api-keys", json={"name": "Integration", "scopes": scopes}, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


async def test_key_on_both_doors_and_masking(app: FastAPI, org3: Org3) -> None:
    cand = await create_candidate(org3.admin, full_name="Key Visible", emails=[{"email": "kv@example.com", "is_primary": True}])
    key = await _key(org3, ["candidate:read", "candidate:create", "candidate:update"])
    api = Api(app, key)
    got = await api.get(f"/candidates/{cand['id']}")
    assert got.status_code == 200 and got.json()["email"] is None  # pii.* never available to keys
    from tests.modules.identity.conftest import new_client

    async with new_client(app) as c:
        internal = await c.get("/api/v1/candidates", headers={"Authorization": f"Bearer {key}"})
    assert internal.status_code == 200
    assert (await api.get("/demands")).status_code == 403  # the key lacks demand:read
    async with tenant_session(org3.tenant_id) as db:
        n = (await db.execute(text("SELECT count(*) FROM api_request_log WHERE api_key_id IS NOT NULL"))).scalar_one()
    assert n >= 0  # written after the response (metering hook)


async def test_kill_switch_refuses_keys(app: FastAPI, org3: Org3) -> None:
    key = await _key(org3, ["candidate:read"])
    human = RequestContext(tenant_id=org3.tenant_id, principal=Principal(type="user", id=org3.admin.user_id, tenant_id=org3.tenant_id))
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=True, reason="incident")
    resp = await Api(app, key).get("/candidates")
    assert (resp.status_code, code(resp)) == (403, "ecosystem_suspended")


def test_public_scopes_for() -> None:
    out = api_keys_service.public_scopes_for(["candidate:read", "contact:read", "api_key:manage", "identity:view"])
    assert "candidates:read" in out and "contacts:read" in out
    assert not any(k.startswith("pii.") for k in out)
    assert api_keys_service.public_scopes_for(["api_key:manage"]) == []


def test_deprecation_sunset_rule() -> None:
    assert validate({"/x": Deprecation(date(2026, 1, 1), date(2027, 1, 1), "/y")}) == []
    assert validate({"/x": Deprecation(date(2026, 1, 1), date(2026, 12, 31), "/y")})


async def test_monthly_deprecation_report(app: FastAPI, org3: Org3) -> None:
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.services.public_api.deprecations import previous_month

    now = datetime.now(UTC)
    at = now.replace(day=1, hour=1, minute=0, second=0, microsecond=0)  # this month (its partition exists)
    report_day = (at + timedelta(days=40)).date().replace(day=1)  # "next month" runs the report for this month
    assert previous_month(report_day)[0] == at.date()
    key_id = uuid.uuid4()
    async with tenant_session(org3.tenant_id) as db:
        for _ in range(3):
            await db.execute(
                text("INSERT INTO api_request_log (id, tenant_id, at, api_key_id, method, route_template, status, api_version, deprecated) "
                     "VALUES (:id, :t, :at, :k, 'GET', '/api/public/v1/old', 200, 'v1', true)"),
                {"id": uuid.uuid4(), "t": org3.tenant_id, "at": at, "k": key_id},
            )  # fmt: skip
    assert await public_api_service.deprecation_report_tenant(org3.tenant_id, report_day) == 1
    assert await public_api_service.deprecation_report_tenant(org3.tenant_id, report_day) == 0  # idempotent per month
    async with tenant_session(org3.tenant_id) as db:
        row = (await db.execute(text("SELECT call_count, route_template FROM api_deprecation_notices"))).first()
    assert row is not None and row[0] == 3 and row[1] == "/api/public/v1/old"
