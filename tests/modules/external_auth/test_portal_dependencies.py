"""AD-10 / AD-26 / AD-27: client, vendor and worker portal dependencies and sessions (Epic 13, story 13.5)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.core.database import platform_session, tenant_session
from app.core.security import CLIENT_COOKIE, SESSION_COOKIES, VENDOR_COOKIE
from app.dependencies.client import ClientDBSessionDep, ClientDep, require_portal_permission
from app.models.identity import Session as AuthSession
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import DOrg
from tests.p2 import (
    External,
    make_client_user,
    make_vendor,
    make_vendor_user,
    sign_in_candidate,
    sign_in_external,
)

CLIENT_ME = "/api/v1/client-portal/me"
VENDOR_ME = "/api/v1/vendor-portal/me"
WORK_ME = "/api/v1/portal/work/me"


@pytest.fixture(scope="module")
def portal_app() -> FastAPI:
    from typing import Annotated

    from fastapi import Depends

    from app.main import create_app

    app = create_app()

    @app.post("/api/v1/client-portal/_t13/touch")
    async def touch(principal: ClientDep, db: ClientDBSessionDep) -> dict[str, Any]:
        rows = (await db.execute(text("SELECT count(*) FROM client_users"))).scalar_one()
        return {"ok": True, "visible_client_users": int(rows), "org": str(principal.org_id)}

    @app.get("/api/v1/client-portal/_t13/timesheets")
    async def needs_perm(_: Annotated[Any, Depends(require_portal_permission("time:approve"))]) -> dict[str, bool]:
        return {"ok": True}

    return app


async def _client_user(app: FastAPI, org: DOrg, **kw: Any) -> External:
    cu = await make_client_user(org.tenant_id, org.client_id, **kw)
    return await sign_in_external(app, "client_user", cu, org.tenant_id, org.client_id)


async def _age_session(token_principal: uuid.UUID, *, idle: timedelta | None = None, absolute: timedelta | None = None) -> None:
    async with platform_session() as pdb:
        values: dict[str, Any] = {}
        now = datetime.now(UTC)
        if idle is not None:
            values["last_seen_at"] = now - idle
        if absolute is not None:
            values["created_at"] = now - absolute
        await pdb.execute(update(AuthSession).where(AuthSession.principal_id == token_principal).values(**values))


async def test_client_me(portal_app: FastAPI, dorg: DOrg) -> None:
    ext = await _client_user(portal_app, dorg, permissions=("time:approve", "invoices:view"))
    try:
        resp = await ext.client.get(CLIENT_ME)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["principal_type"] == "client_user" and body["org_kind"] == "client"
        assert body["org_id"] == str(dorg.client_id) and body["portal_permissions"] == ["invoices:view", "time:approve"]
        # The client cookie is not a vendor cookie, and an internal user cookie is not a client cookie.
        assert (await ext.client.get(VENDOR_ME)).status_code == 401
        assert (await dorg.admin.client.get(CLIENT_ME)).status_code == 401
        assert (await ext.client.get("/api/v1/_t13/none")).status_code == 404
        perm = await ext.client.get("/api/v1/client-portal/_t13/timesheets")
        assert perm.status_code == 200
    finally:
        await ext.client.aclose()


async def test_portal_permission_required(portal_app: FastAPI, dorg: DOrg) -> None:
    ext = await _client_user(portal_app, dorg, permissions=("invoices:view",))
    try:
        resp = await ext.client.get("/api/v1/client-portal/_t13/timesheets")
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "portal_permission_required"
    finally:
        await ext.client.aclose()


async def test_client_db_session_is_org_scoped(portal_app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    ext = await _client_user(portal_app, dorg)
    # A second client of the same tenant with its own Client User.
    other_client = await dorg.admin.client.post("/api/v1/clients", json={"name": f"Other {uuid.uuid4().hex[:6]}"}, headers=dorg.admin.h())
    await make_client_user(dorg.tenant_id, uuid.UUID(other_client.json()["id"]))
    try:
        resp = await ext.client.post("/api/v1/client-portal/_t13/touch", headers=ext.h())
        assert resp.status_code == 200, resp.text
        assert resp.json()["visible_client_users"] == 1  # only its own client's users
    finally:
        await ext.client.aclose()
    assert other_dorg  # another tenant entirely: nothing to see either


async def test_csrf_required_and_checked_before_touch(portal_app: FastAPI, dorg: DOrg) -> None:
    ext = await _client_user(portal_app, dorg)
    try:
        await _age_session(ext.id, idle=timedelta(minutes=5))
        async with platform_session() as pdb:
            before = (await pdb.execute(select(AuthSession.last_seen_at).where(AuthSession.principal_id == ext.id))).scalar_one()
        resp = await ext.client.post("/api/v1/client-portal/_t13/touch")
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "csrf_failed"
        async with platform_session() as pdb:
            after = (await pdb.execute(select(AuthSession.last_seen_at).where(AuthSession.principal_id == ext.id))).scalar_one()
        assert after == before  # not touched on a CSRF failure
        ok = await ext.client.post("/api/v1/client-portal/_t13/touch", headers=ext.h())
        assert ok.status_code == 200
    finally:
        await ext.client.aclose()


@pytest.mark.parametrize(("idle", "absolute"), [(timedelta(minutes=31), None), (None, timedelta(hours=13))])
async def test_expired_sessions(portal_app: FastAPI, dorg: DOrg, idle: timedelta | None, absolute: timedelta | None) -> None:
    ext = await _client_user(portal_app, dorg)
    try:
        await _age_session(ext.id, idle=idle, absolute=absolute)
        resp = await ext.client.get(CLIENT_ME)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "session_expired"
        assert CLIENT_COOKIE in resp.headers.get("set-cookie", "")
    finally:
        await ext.client.aclose()


async def test_revoked_and_suspended_client_users(portal_app: FastAPI, dorg: DOrg) -> None:
    ext = await _client_user(portal_app, dorg)
    try:
        async with platform_session() as pdb:
            await pdb.execute(update(AuthSession).where(AuthSession.principal_id == ext.id).values(revoked_at=datetime.now(UTC)))
        assert (await ext.client.get(CLIENT_ME)).status_code == 401
    finally:
        await ext.client.aclose()
    ext = await _client_user(portal_app, dorg)
    try:
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(text("UPDATE client_users SET status = 'suspended' WHERE id = :id"), {"id": ext.id})
        assert (await ext.client.get(CLIENT_ME)).status_code == 401
        async with platform_session() as pdb:
            live = (await pdb.execute(select(AuthSession).where(AuthSession.principal_id == ext.id, AuthSession.revoked_at.is_(None)))).first()
        assert live is None  # every session of the suspended principal is revoked
    finally:
        await ext.client.aclose()


async def test_vendor_me_and_vendor_status(portal_app: FastAPI, dorg: DOrg) -> None:
    vendor = await make_vendor(dorg.tenant_id, status="onboarding")
    vu = await make_vendor_user(dorg.tenant_id, vendor)
    ext = await sign_in_external(portal_app, "vendor_user", vu, dorg.tenant_id, vendor)
    try:
        resp = await ext.client.get(VENDOR_ME)
        assert resp.status_code == 200, resp.text
        assert resp.json()["org_kind"] == "vendor" and resp.json()["org_id"] == str(vendor)
        assert (await ext.client.get(CLIENT_ME)).status_code == 401
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(text("UPDATE vendors SET status = 'suspended' WHERE id = :id"), {"id": vendor})
        assert (await ext.client.get(VENDOR_ME)).status_code == 401
        async with platform_session() as pdb:
            live = (await pdb.execute(select(AuthSession).where(AuthSession.principal_id == vu, AuthSession.revoked_at.is_(None)))).first()
        assert live is None
    finally:
        await ext.client.aclose()
    assert SESSION_COOKIES["vendor_user"] == VENDOR_COOKIE


async def test_worker_me_uses_candidate_principal(portal_app: FastAPI, dorg: DOrg) -> None:
    candidate = await create_candidate(dorg.admin, full_name="Wanda Worker")
    client: httpx.AsyncClient = await sign_in_candidate(portal_app, uuid.UUID(candidate["id"]), dorg.tenant_id)
    try:
        resp = await client.get(WORK_ME)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["principal_type"] == "candidate" and body["org_kind"] == "worker"
        assert body["org_id"] == candidate["id"] and body["display_name"] == "Wanda Worker"
        # A user cookie is not a worker.
        assert (await dorg.admin.client.get(WORK_ME)).status_code == 401
    finally:
        await client.aclose()
