# ruff: noqa: E501
"""Epic 74: accounts (no enumeration, single-use links, forced MFA, TOTP replay, developer cookie refused on tenant
routes, reset revokes sessions), orgs (roles, invitations, last owner, audit), Verified Partner (GSTIN, reviewer role,
★ approve), sandboxes (limit, handoff single use, sandbox_only, cross-org 404), conformance (well-behaved App passes and
the report verifies with 69's verifier; ignoring Retry-After fails; production untouched) and submissions."""

import uuid
from typing import Any

import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text, update

from app.core.database import platform_session
from app.core.tenancy import clear_sandbox_cache
from app.models.identity import PlatformOperator
from app.services.developer_portal.conformance import fault_injection, runner
from app.services.marketplace import conformance_report
from tests.developer_portal.conftest import (
    DEV,
    PASSWORD,
    Dev,
    email_token,
    install,
    make_dev,
    make_org,
    partner_app,
    playground_token,
    ready_sandbox,
    signing_key,
    signup_verified,
)
from tests.modules.identity.conftest import new_client

GSTIN_OK = "27AAPFU0939F1ZV"


async def test_accounts_security(app: FastAPI) -> None:
    dev = await make_dev(app)
    resp = await new_client(app).post(f"{DEV}/auth/signup", json={"email": dev.email, "name": "X", "password": PASSWORD, "accept_terms_version": "2026-10"})
    assert resp.status_code == 202
    assert (await dev.client.get("/api/v1/candidates")).status_code == 401
    client, email = await signup_verified(app)
    assert (await client.post(f"{DEV}/auth/verify-email", json={"token": email_token("developer_verify_email", email)})).status_code == 410
    resp = await client.post(f"{DEV}/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.json()["step"] == "enroll_required"
    assert (await client.get(f"{DEV}/me")).status_code == 401
    login = new_client(app)
    await login.post(f"{DEV}/auth/login", json={"email": dev.email, "password": PASSWORD})
    h = {"X-CSRF-Token": login.cookies.get("tos_csrf") or ""}
    resp = await login.post(f"{DEV}/auth/mfa/verify", json={"code": pyotp.TOTP(dev.secret).now()}, headers=h)
    assert resp.status_code == 401, "TOTP replay (the enrolment step was already used)"
    await new_client(app).post(f"{DEV}/auth/password-reset/request", json={"email": dev.email})
    token = email_token("developer_password_reset", dev.email)
    resp = await new_client(app).post(f"{DEV}/auth/password-reset/confirm", json={"token": token, "password": "Another-Voyage-77"})
    assert resp.status_code == 204
    assert (await dev.client.get(f"{DEV}/me")).status_code == 401


async def test_orgs_roles_invitations_and_verification(app: FastAPI, mfa_operator: Any) -> None:
    owner = await make_dev(app)
    org = await make_org(owner)
    colleague = await make_dev(app)
    resp = await owner.client.post(f"{DEV}/orgs/{org}/invitations", json={"email": colleague.email, "role": "viewer"}, headers=owner.h())
    assert resp.status_code == 201, resp.text
    token = email_token("developer_invitation", colleague.email)
    stranger = await make_dev(app)
    assert (await stranger.client.post(f"{DEV}/invitations/accept", json={"token": token}, headers=stranger.h())).status_code == 409
    resp = await colleague.client.post(f"{DEV}/invitations/accept", json={"token": token}, headers=colleague.h())
    assert resp.status_code == 200, resp.text
    colleague.org_id = org
    resp = await colleague.client.post(f"{DEV}/orgs/{org}/apps", json={"name": "Nope app", "requested_scopes": ["candidates:read"]}, headers=colleague.h())
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "org_role_required", resp.text
    assert (await stranger.client.get(f"{DEV}/orgs/{org}/members", headers=stranger.h(org))).status_code == 403
    resp = await owner.client.patch(f"{DEV}/orgs/{org}/members/{owner.account_id}", json={"role": "developer"}, headers=owner.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "last_owner"
    base = {"legal_name": "Acme Integrations Pvt Ltd", "country": "IN", "tax_id_kind": "gstin", "website": "https://acme.example.com",
            "registered_address": "1 MG Road, Pune", "support_email": "support@acme.example.com", "agreement_version": "2026-10",
            "security_questionnaire": {"data_location": "India", "incident_contact": "sec@acme.example.com"}}  # fmt: skip
    resp = await owner.client.put(f"{DEV}/orgs/{org}/verification", json={**base, "tax_id": "27AAPFU0939F1ZX"}, headers=owner.h())
    assert resp.status_code == 422
    assert (await owner.client.put(f"{DEV}/orgs/{org}/verification", json={**base, "tax_id": GSTIN_OK}, headers=owner.h())).status_code == 200
    resp = await owner.client.post(f"{DEV}/orgs/{org}/verification/submit", headers=owner.h())
    assert resp.status_code == 200 and resp.json()["status"] == "pending", resp.text
    url = f"/api/v1/platform/developers/verification-requests/{resp.json()['id']}/decide"
    resp = await mfa_operator.client.post(url, json={"decision": "approve"}, headers=mfa_operator.h())
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "platform_role_required"
    async with platform_session() as db:
        await db.execute(update(PlatformOperator).where(PlatformOperator.id == mfa_operator.id).values(roles=["marketplace_reviewer"]))
    resp = await mfa_operator.client.post(url, json={"decision": "approve", "message": "Welcome"}, headers=mfa_operator.h())
    assert resp.status_code == 200, resp.text
    view = (await owner.client.get(f"{DEV}/orgs/{org}/verification", headers=owner.h())).json()
    assert view["org_verification_status"] == "verified" and view["verification_expires_at"] and view["message"] == "Welcome"
    actions = {r["action"] for r in (await owner.client.get(f"{DEV}/orgs/{org}/audit", headers=owner.h())).json()}
    assert {"org.created", "org.member_invited", "org.member_joined", "org.verification_submitted", "org.verification_decided"} <= actions


async def _sandbox_app(app: FastAPI) -> tuple[Dev, dict[str, Any], dict[str, Any], str]:
    dev = await make_dev(app)
    await make_org(dev, verified=True)
    sandbox = await ready_sandbox(dev)
    created = await partner_app(dev)
    inst = await install(dev, sandbox["id"], created["id"], ["candidates:read"])
    assert inst["status"] == "active", inst
    return dev, sandbox, created, await playground_token(dev, sandbox["id"], created)


async def test_sandbox_limits_handoff_and_sandbox_only(app: FastAPI, fast_sandboxes: None) -> None:
    dev, sandbox, created, _ = await _sandbox_app(app)
    for i in range(2):
        await ready_sandbox(dev, f"Extra {i}")
    resp = await dev.client.post(f"{DEV}/orgs/{dev.org_id}/sandboxes", json={"name": "Fourth"}, headers=dev.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "sandbox_limit"
    url = (await dev.client.post(f"{DEV}/sandboxes/{sandbox['id']}/open", headers=dev.h())).json()["url"]
    t = url.split("#t=")[1]
    browser = new_client(app)
    resp = await browser.post(f"{DEV}/sandbox-handoff", json={"t": t})
    assert resp.status_code == 200 and resp.json()["redirect"] == "/dashboard", resp.text
    assert (await browser.get("/api/v1/auth/me")).status_code == 200
    assert (await new_client(app).post(f"{DEV}/sandbox-handoff", json={"t": t})).status_code == 410
    other = await make_dev(app)
    await make_org(other)
    assert (await other.client.post(f"{DEV}/sandboxes/{sandbox['id']}/open", headers=other.h())).status_code == 404
    async with platform_session() as db:
        await db.execute(text("UPDATE tenants SET mode='production', sandbox_owner_kind=NULL, sandbox_owner_id=NULL WHERE id=:t"), {"t": sandbox["tenant_id"]})
    clear_sandbox_cache(sandbox["tenant_id"])
    resp = await dev.client.post(f"{DEV}/playground/token", json={"app_id": created["id"], "sandbox_id": sandbox["id"], "client_secret": created["client_secret"]}, headers=dev.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "sandbox_only"


async def _run(dev: Dev, sandbox: dict[str, Any], created: dict[str, Any]) -> str:
    resp = await dev.client.post(f"{DEV}/apps/{created['id']}/conformance-runs", json={"sandbox_id": sandbox["id"]}, headers=dev.h())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_conformance_and_submission(app: FastAPI, fast_sandboxes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    await signing_key()
    monkeypatch.setattr(fault_injection, "FAULT_429_AFTER_SECONDS", 0.0)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(fault_injection, "clock", lambda: clock["t"])
    dev, sandbox, created, token = await _sandbox_app(app)
    api = new_client(app)
    bearer = {"Authorization": f"Bearer {token}"}
    run_id = await _run(dev, sandbox, created)
    resp = await api.get("/api/public/v1/candidates", headers=bearer)
    assert resp.status_code == 429 and resp.headers["retry-after"] == "5", resp.text
    clock["t"] += 6
    assert (await api.get("/api/public/v1/candidates", headers=bearer)).status_code == 200
    clock["t"] += 1
    resp = await dev.client.post(f"{DEV}/conformance-runs/{run_id}/finish", headers=dev.h())
    assert resp.status_code == 200, resp.text
    out = await runner.finalize_run(uuid.UUID(run_id))
    assert out is not None and out.status == "passed", out
    report = (await dev.client.get(f"{DEV}/conformance-runs/{run_id}/report", headers=dev.h())).json()
    assert await conformance_report.verify(report, app_id=uuid.UUID(created["id"]), manifest_sha256=report["manifest_sha256"]) == []
    resp = await dev.client.post(f"{DEV}/apps/{created['id']}/listing", json={"slug": f"acme-{run_id[:8]}", "name": "Acme ATS Sync", "tagline": "Sync candidates with Acme",
                                 "category": "other", "support_url": "https://acme.example.com/support"}, headers=dev.h())  # fmt: skip
    assert resp.status_code == 201, resp.text
    resp = await dev.client.put(f"{DEV}/listings/{resp.json()['id']}/versions", json={"description_md": "Keeps Acme in step.", "privacy_url": "https://acme.example.com/p",
                                "dpa_url": "https://acme.example.com/dpa", "changelog": "First"}, headers=dev.h())  # fmt: skip
    assert resp.status_code == 200, resp.text
    vid = resp.json()["id"]
    from app.services.marketplace import submissions as mkt

    monkeypatch.setattr(mkt, "_kicker", None)
    resp = await dev.client.post(f"{DEV}/versions/{vid}/submit", json={"conformance_run_id": str(uuid.uuid4())}, headers=dev.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conformance_report_required"
    resp = await dev.client.post(f"{DEV}/versions/{vid}/submit", json={"conformance_run_id": run_id}, headers=dev.h())
    assert resp.status_code == 200 and resp.json()["status"] == "submitted", resp.text
    subs = (await dev.client.get(f"{DEV}/orgs/{dev.org_id}/submissions", headers=dev.h())).json()
    assert subs and subs[0]["app_name"] == "Acme ATS Sync"


async def test_ignoring_retry_after_fails_and_production_untouched(app: FastAPI, fast_sandboxes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    await signing_key()
    monkeypatch.setattr(fault_injection, "FAULT_429_AFTER_SECONDS", 0.0)
    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(fault_injection, "clock", lambda: clock["t"])
    dev, sandbox, created, token = await _sandbox_app(app)
    api = new_client(app)
    bearer = {"Authorization": f"Bearer {token}"}
    run_id = await _run(dev, sandbox, created)
    from tests.modules.identity.conftest import make_tenant
    from tests.support.oauth import make_app, mint

    prod = await make_tenant()
    papp = await make_app()
    from tests.support.oauth import install_app

    owner_user = await _any_user(prod.id)
    pinst = await install_app(prod.id, papp, granted_by=owner_user)
    ptoken = (await mint(pinst)).access_token
    index = await fault_injection.kv_get(fault_injection.INDEX) or {}
    index[str(prod.id)] = [str(pinst.id)]
    await fault_injection.kv_set(fault_injection.INDEX, index, 600)
    await fault_injection.kv_set(fault_injection.state_key(pinst.id), {"tenant_id": str(prod.id), "started": 0, "calls": []}, 600)
    assert await fault_injection.resolve("GET", "/api/public/v1/candidates", {"authorization": f"Bearer {ptoken}"}) is None
    assert (await api.get("/api/public/v1/candidates", headers=bearer)).status_code == 429
    clock["t"] += 1
    resp = await api.get("/api/public/v1/candidates", headers=bearer)
    assert resp.status_code == 200, resp.text
    clock["t"] += 1
    await dev.client.post(f"{DEV}/conformance-runs/{run_id}/finish", headers=dev.h())
    out = await runner.finalize_run(uuid.UUID(run_id))
    assert out is not None and out.status == "failed"
    assert {c.key for c in out.checks if not c.passed} == {"handles_429"}, out.checks


async def _any_user(tenant_id: uuid.UUID) -> uuid.UUID:
    from app.core.database import tenant_session
    from app.models.identity import User

    async with tenant_session(tenant_id) as db:
        user = User(id=uuid.uuid4(), tenant_id=tenant_id, email=f"u-{uuid.uuid4().hex[:8]}@example.com", name="Owner", password_hash="!")
        db.add(user)
        await db.flush()
        return user.id
