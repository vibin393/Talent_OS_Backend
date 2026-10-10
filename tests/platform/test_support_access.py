"""Story 12.5 (FR-69): support access requests, tenant approval, read-only support sessions with a
pre-committed audit row per request, revoke / expiry. Matrix rows 18-20, 35."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import update

from app.core.database import tenant_session
from app.models.platform import SupportAccessGrant
from app.services.support_access_service import SUPPORT_COOKIE, support_permissions
from tests.epic12 import PLATFORM, PRIVACY, Operator, Org3, create_candidate, h, member, sent
from tests.modules.identity.conftest import new_client

REASON = "Customer reported a broken candidate import (ticket)"


async def _request(op: Operator, org: Org3, hours: int = 4) -> dict[str, object]:
    resp = await op.client.post(
        f"{PLATFORM}/tenants/{org.tenant_id}/support-requests",
        json={"reason": REASON, "ticket_ref": "SUP-42", "requested_hours": hours},
        headers=op.h(),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_request_validation(mfa_operator: Operator, org3: Org3) -> None:
    """Matrix 20: 30 hours -> 422; short reason -> 422."""
    too_long = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{org3.tenant_id}/support-requests",
        json={"reason": REASON, "requested_hours": 30},
        headers=mfa_operator.h(),
    )
    assert too_long.status_code == 422
    short = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{org3.tenant_id}/support-requests",
        json={"reason": "help", "requested_hours": 2},
        headers=mfa_operator.h(),
    )
    assert short.status_code == 422
    missing = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{uuid.uuid4()}/support-requests",
        json={"reason": REASON, "requested_hours": 2},
        headers=mfa_operator.h(),
    )
    assert missing.status_code == 404


async def test_full_support_flow(app: FastAPI, mfa_operator: Operator, org3: Org3) -> None:
    """Matrix 18 / 19: read-only support browsing, audited; revoke ends access at once."""
    cand = await create_candidate(org3.admin)
    grant = await _request(mfa_operator, org3)
    assert grant["status"] == "pending"
    assert sent("support_access_requested")
    early = await mfa_operator.client.post(f"{PLATFORM}/support-requests/{grant['id']}/session", headers=mfa_operator.h())
    assert (early.status_code, early.json()["error"]["code"]) == (409, "support_not_approved")

    listed = (await org3.admin.client.get(f"{PRIVACY}/support-access")).json()
    assert [g["id"] for g in listed] == [grant["id"]]
    approved = await org3.admin.client.post(f"{PRIVACY}/support-access/{grant['id']}/approve", headers=h(org3.admin))
    assert approved.json()["status"] == "approved"
    expires = datetime.fromisoformat(approved.json()["expires_at"])
    assert timedelta(hours=3, minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=4)

    session = await mfa_operator.client.post(f"{PLATFORM}/support-requests/{grant['id']}/session", headers=mfa_operator.h())
    assert session.status_code == 200, session.text
    token = mfa_operator.client.cookies.get(SUPPORT_COOKIE)
    assert token
    async with new_client(app) as support:
        support.cookies.set(SUPPORT_COOKIE, token)
        read = await support.get(f"/api/v1/candidates/{cand['id']}")
        assert read.status_code == 200, read.text
        write = await support.post("/api/v1/candidates", json={"full_name": "Nope"})
        assert (write.status_code, write.json()["error"]["code"]) == (403, "support_read_only")
        admin_only = await support.get("/api/v1/admin/audit-logs")
        assert admin_only.status_code == 200  # audit:read is a *:read permission
        audit_rows = (
            await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "support.request"})
        ).json()
        assert audit_rows["total"] >= 2  # the two GETs (the refused POST never reached a handler)
        entry = audit_rows["items"][-1]
        assert (entry["actor_type"], entry["data"]["grant_id"]) == ("platform", grant["id"])
        assert entry["data"]["method"] == "GET"

        revoked = await org3.admin.client.post(f"{PRIVACY}/support-access/{grant['id']}/revoke", headers=h(org3.admin))
        assert revoked.json()["status"] == "revoked"
        after = await support.get(f"/api/v1/candidates/{cand['id']}")
        assert (after.status_code, after.json()["error"]["code"]) == (401, "support_access_ended")


async def test_expired_grant_ends_access(app: FastAPI, mfa_operator: Operator, org3: Org3) -> None:
    """Matrix 19 (expiry) + the ``expire_support_grants`` job."""
    grant = await _request(mfa_operator, org3, hours=1)
    await org3.admin.client.post(f"{PRIVACY}/support-access/{grant['id']}/approve", headers=h(org3.admin))
    await mfa_operator.client.post(f"{PLATFORM}/support-requests/{grant['id']}/session", headers=mfa_operator.h())
    token = mfa_operator.client.cookies.get(SUPPORT_COOKIE)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(
            update(SupportAccessGrant)
            .where(SupportAccessGrant.id == uuid.UUID(str(grant["id"])))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    async with new_client(app) as support:
        support.cookies.set(SUPPORT_COOKIE, token or "")
        resp = await support.get("/api/v1/candidates")
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "support_access_ended")
    from app.tasks.platform import expire_support_grants

    expire_support_grants.delay(str(org3.tenant_id))
    listed = (await org3.admin.client.get(f"{PRIVACY}/support-access")).json()
    assert listed[0]["status"] == "expired"


async def test_deny_cancel_and_cross_tenant(app: FastAPI, mfa_operator: Operator, org3: Org3, other3: Org3) -> None:
    g1 = await _request(mfa_operator, org3)
    g2 = await _request(mfa_operator, org3)
    denied = await org3.admin.client.post(f"{PRIVACY}/support-access/{g1['id']}/deny", headers=h(org3.admin))
    assert denied.json()["status"] == "denied"
    cancelled = await mfa_operator.client.post(f"{PLATFORM}/support-requests/{g2['id']}/cancel", headers=mfa_operator.h())
    assert cancelled.json()["status"] == "cancelled"
    for action in ("approve", "deny", "revoke"):
        resp = await other3.admin.client.post(f"{PRIVACY}/support-access/{g1['id']}/{action}", headers=h(other3.admin))
        assert resp.status_code == 404
    assert (await other3.admin.client.get(f"{PRIVACY}/support-access")).json() == []
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(f"{PRIVACY}/support-access")).status_code == 403
    finally:
        await recruiter.client.aclose()


async def test_operator_endpoints_need_mfa(app: FastAPI, org3: Org3) -> None:
    from app.services import auth_service as identity
    from tests.modules.identity.conftest import PASSWORD

    email = f"nomfa-{uuid.uuid4().hex[:8]}@example.com"
    await identity.create_operator(email, "No MFA", PASSWORD)
    async with new_client(app) as client:
        await client.post("/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD})
        resp = await client.get(f"{PLATFORM}/support-requests")
        assert (resp.status_code, resp.json()["error"]["code"]) == (403, "mfa_enrollment_required")
        # Tenant cookies never authenticate operator routes.
        client.cookies.clear()
        client.cookies.update(org3.admin.client.cookies)
        assert (await client.get(f"{PLATFORM}/support-requests")).status_code == 401


def test_support_permissions_are_read_only() -> None:
    perms = support_permissions()
    assert perms and all(p.endswith(":read") for p in perms)
    assert "compensation:view" not in perms
    assert not any(p.endswith(":export") for p in perms)


async def test_auth_me_for_support_session(app: FastAPI, mfa_operator: Operator, org3: Org3) -> None:
    """``GET /auth/me`` answers a support session (principal_type="support", operator + expiry, read-only
    permissions); normal users keep principal_type="user" and no support block."""
    grant = await _request(mfa_operator, org3)
    approved = await org3.admin.client.post(f"{PRIVACY}/support-access/{grant['id']}/approve", headers=h(org3.admin))
    session = await mfa_operator.client.post(f"{PLATFORM}/support-requests/{grant['id']}/session", headers=mfa_operator.h())
    assert session.status_code == 200, session.text
    token = mfa_operator.client.cookies.get(SUPPORT_COOKIE)
    assert token

    async with new_client(app) as support:
        support.cookies.set(SUPPORT_COOKIE, token)
        me = await support.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["principal_type"] == "support"
        assert body["support"]["operator_name"]
        assert datetime.fromisoformat(body["support"]["expires_at"]) == datetime.fromisoformat(
            approved.json()["expires_at"]
        )
        assert body["tenant"]["id"] == str(org3.tenant_id)
        assert body["user"]["role_keys"] == []
        assert body["mfa_enrollment_required"] is False
        perms = body["permissions"]
        assert perms
        assert all(p.endswith(":read") for p in perms)
        assert "compensation:view" not in perms
        # Still read-only.
        write = await support.post("/api/v1/candidates", json={"full_name": "Nope"})
        assert (write.status_code, write.json()["error"]["code"]) == (403, "support_read_only")

        revoked = await org3.admin.client.post(f"{PRIVACY}/support-access/{grant['id']}/revoke", headers=h(org3.admin))
        assert revoked.json()["status"] == "revoked"
        after = await support.get("/api/v1/auth/me")
        assert (after.status_code, after.json()["error"]["code"]) == (401, "support_access_ended")

    user_me = await org3.admin.client.get("/api/v1/auth/me")
    assert user_me.status_code == 200
    assert user_me.json()["principal_type"] == "user"
    assert user_me.json()["support"] is None
