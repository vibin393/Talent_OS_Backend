"""Story 12.6 (FR-70..FR-72): tenant suspension / offboarding / deletion, sub-processors, incident notices and
platform notices. Matrix rows 21-24, 35."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.core import secure_links
from app.core.database import platform_session, tenant_session
from app.models.identity import Tenant
from app.models.platform import TenantDeletionCertificate, TenantExport
from app.services import platform_service
from tests.epic12 import PLATFORM, Operator, Org3, create_candidate, h, sent
from tests.modules.identity.conftest import USER_PASSWORD, new_client

REASON = "Unpaid invoices for three months"


async def _post(op: Operator, url: str, body: dict[str, object]) -> dict[str, object]:
    resp = await op.client.post(url, json=body, headers=op.h())
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_suspend_blocks_access_keeps_data_then_reactivate(
    app: FastAPI, mfa_operator: Operator, org3: Org3
) -> None:
    """Matrix 21 (login 403, API key 403, career apply 409 via ``is_accepting_applications``, links 410)."""
    cand = await create_candidate(org3.admin)
    key = await org3.admin.client.post(
        "/api/v1/api-keys", json={"name": "ATS sync", "scopes": ["candidate:read"]}, headers=h(org3.admin)
    )
    assert key.status_code == 201, key.text
    bearer = {"Authorization": f"Bearer {key.json()['key']}"}
    async with tenant_session(org3.tenant_id) as db:
        _, token = await secure_links.issue(
            db, tenant_id=org3.tenant_id, purpose="consent_request", subject_type="candidate",
            subject_id=uuid.UUID(cand["id"]), ttl=timedelta(days=1), metadata={"purpose": "ai_processing"},
        )  # fmt: skip
    short = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{org3.tenant_id}/suspend", json={"reason": "short"}, headers=mfa_operator.h()
    )
    assert short.status_code == 422
    out = await _post(mfa_operator, f"{PLATFORM}/tenants/{org3.tenant_id}/suspend", {"reason": REASON})
    assert out["status"] == "suspended"

    assert (await org3.admin.client.get("/api/v1/auth/me")).status_code == 401  # session revoked
    login = await org3.admin.client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": org3.tenant.slug, "email": org3.admin.email, "password": USER_PASSWORD},
    )
    assert (login.status_code, login.json()["error"]["code"]) == (403, "tenant_suspended")
    async with new_client(app) as client:
        api = await client.get("/api/v1/candidates", headers=bearer)
        assert (api.status_code, api.json()["error"]["code"]) == (403, "tenant_suspended")
        link = await client.get(f"/api/v1/public/links/{token}")
        assert (link.status_code, link.json()["error"]["code"]) == (410, "link_unavailable")
    assert await platform_service.is_accepting_applications(org3.tenant_id) is False
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM candidates"))).scalar_one() == 1  # data intact

    again = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{org3.tenant_id}/suspend", json={"reason": REASON}, headers=mfa_operator.h()
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "invalid_transition")
    out = await _post(mfa_operator, f"{PLATFORM}/tenants/{org3.tenant_id}/reactivate", {"reason": "Invoices settled now"})
    assert out["status"] == "active"
    assert await platform_service.is_accepting_applications(org3.tenant_id) is True
    audit = (await (await _admin_client(app, org3)).get("/api/v1/admin/audit-logs", params={"action": "tenant."})).json()
    assert {"tenant.suspended", "tenant.reactivated"} <= {e["action"] for e in audit["items"]}


async def _admin_client(app: FastAPI, org: Org3) -> httpx.AsyncClient:
    client = new_client(app)
    resp = await client.post(
        "/api/v1/auth/login", json={"tenant_slug": org.tenant.slug, "email": org.admin.email, "password": USER_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return client


async def test_offboarding_export_read_only_and_deletion(
    app: FastAPI, mfa_operator: Operator, org3: Org3, _isolated_files: Path
) -> None:
    """Matrix 22: export link, admins read-only during grace, deletion after 30 days + certificate."""
    await create_candidate(org3.admin)
    bad = await mfa_operator.client.post(
        f"{PLATFORM}/tenants/{org3.tenant_id}/offboard",
        json={"reason": REASON, "contact_email": "dpo@initech.example.com", "confirm_slug": "wrong"},
        headers=mfa_operator.h(),
    )
    assert bad.status_code == 422
    out = await _post(
        mfa_operator,
        f"{PLATFORM}/tenants/{org3.tenant_id}/offboard",
        {"reason": REASON, "contact_email": "dpo@initech.example.com", "confirm_slug": org3.tenant.slug},
    )
    assert out["status"] == "offboarding"
    detail = (await mfa_operator.client.get(f"{PLATFORM}/tenants/{org3.tenant_id}/overview")).json()
    assert detail["exports"][0]["status"] == "ready"
    (mail,) = sent("tenant_export", to="dpo@initech.example.com")
    token = mail.text.split("/l/")[1].split()[0]
    async with new_client(app) as anon:
        dl = await anon.get(f"/api/v1/public/links/{token}/download")
        assert dl.status_code == 200 and dl.content[:2] == b"PK"

    admin = await _admin_client(app, org3)
    try:
        assert (await admin.get("/api/v1/candidates")).status_code == 200
        write = await admin.post("/api/v1/candidates", json={"full_name": "No"}, headers={"X-CSRF-Token": admin.cookies.get("tos_csrf") or ""})
        assert (write.status_code, write.json()["error"]["code"]) == (403, "tenant_read_only")
    finally:
        await admin.aclose()

    from app.tasks.platform import process_tenant_deletions

    assert process_tenant_deletions.delay().get() == []  # grace period not over
    async with platform_session() as db:
        await db.execute(
            update(Tenant).where(Tenant.id == org3.tenant_id).values(delete_after=datetime.now(UTC) - timedelta(minutes=1))
        )
    assert process_tenant_deletions.delay().get() == [str(org3.tenant_id)]
    async with platform_session() as db:
        tenant = await db.get(Tenant, org3.tenant_id)
        cert = (
            await db.execute(select(TenantDeletionCertificate).where(TenantDeletionCertificate.tenant_id == org3.tenant_id))
        ).scalar_one()
        exports = (await db.execute(select(TenantExport).where(TenantExport.tenant_id == org3.tenant_id))).all()
    assert tenant is not None and tenant.status == "deleted"
    assert cert.row_counts.get("candidates") == 1 and cert.row_counts.get("audit_logs", 0) > 0
    assert exports == []
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0
        assert (await db.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 0
    assert not (_isolated_files / "t" / str(org3.tenant_id)).exists()
    assert sent("tenant_deletion_certificate", to="dpo@initech.example.com")


async def test_cancel_offboarding(mfa_operator: Operator, org3: Org3) -> None:
    await _post(
        mfa_operator,
        f"{PLATFORM}/tenants/{org3.tenant_id}/offboard",
        {"reason": REASON, "contact_email": "dpo@initech.example.com", "confirm_slug": org3.tenant.slug},
    )
    out = await _post(mfa_operator, f"{PLATFORM}/tenants/{org3.tenant_id}/cancel-offboard", {"reason": "Customer renewed"})
    assert (out["status"], out["delete_after"]) == ("active", None)


async def test_sub_processor_notice_period_and_emergency(
    mfa_operator: Operator, org3: Org3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix 23."""
    from app.tasks import platform as platform_tasks

    # The test DB holds many tenants: fan the notice out to this test's tenant only.
    monkeypatch.setattr(
        platform_tasks,
        "enqueue_sub_processor_notice",
        lambda sub_id: platform_tasks.notify_sub_processor.delay(str(sub_id), str(org3.tenant_id)),
    )
    soon = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    body = {
        "name": "SMS Gateway Ltd",
        "purpose": "Interview reminders by SMS",
        "data_categories": ["phone numbers"],
        "processing_location": "Mumbai",
        "country": "India",
        "effective_from": soon,
    }
    resp = await mfa_operator.client.post(f"{PLATFORM}/sub-processors", json=body, headers=mfa_operator.h())
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "notice_period_too_short")
    resp = await mfa_operator.client.post(
        f"{PLATFORM}/sub-processors",
        json={**body, "emergency": True, "emergency_reason": "Primary provider outage"},
        headers=mfa_operator.h(),
    )
    assert resp.status_code == 201, resp.text
    later = (datetime.now(UTC) + timedelta(days=31)).isoformat()
    ok = await mfa_operator.client.post(
        f"{PLATFORM}/sub-processors", json={**body, "name": "Mail Co", "effective_from": later}, headers=mfa_operator.h()
    )
    assert ok.status_code == 201
    public = (await mfa_operator.client.get("/api/v1/public/sub-processors")).json()
    assert {"Anthropic PBC", "SMS Gateway Ltd", "Mail Co"} <= {s["name"] for s in public}
    tenant_view = (await org3.admin.client.get("/api/v1/sub-processors")).json()
    assert "Mail Co" in {s["name"] for s in tenant_view}
    notices = (await org3.admin.client.get("/api/v1/notices")).json()
    assert any(n["kind"] == "sub_processor" and "Mail Co" in n["title"] for n in notices)
    assert sent("sub_processor_notice", to=org3.admin.email)


async def test_incident_notification_late_flag_and_ack(mfa_operator: Operator, org3: Org3, other3: Org3) -> None:
    """Matrix 24: sent 30 h after confirmation -> late; admins see and acknowledge the notice."""
    incident = await mfa_operator.client.post(
        f"{PLATFORM}/incidents",
        json={
            "title": "Misrouted export",
            "description": "An export was emailed to the wrong address.",
            "severity": "high",
            "confirmed_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
            "details": {"data_categories": ["names", "emails"], "mitigation": "Recipient deleted the file."},
        },
        headers=mfa_operator.h(),
    )
    assert incident.status_code == 201, incident.text
    notify = await mfa_operator.client.post(
        f"{PLATFORM}/incidents/{incident.json()['id']}/notify",
        json={"tenant_ids": [str(org3.tenant_id)]},
        headers=mfa_operator.h(),
    )
    assert notify.status_code == 200, notify.text
    (row,) = notify.json()
    assert row["late"] is True
    assert "Data Protection Board" in sent("incident_notice", to=org3.admin.email)[-1].text
    notices = (await org3.admin.client.get("/api/v1/notices")).json()
    (notice,) = [n for n in notices if n["kind"] == "incident"]
    assert notice["acknowledged"] is False
    assert (await org3.admin.client.post(f"/api/v1/notices/{notice['id']}/ack", headers=h(org3.admin))).status_code == 204
    assert next(n for n in (await org3.admin.client.get("/api/v1/notices")).json() if n["kind"] == "incident")["acknowledged"]
    # Another tenant neither sees nor can acknowledge it.
    assert all(n["id"] != notice["id"] for n in (await other3.admin.client.get("/api/v1/notices")).json())
    assert (await other3.admin.client.post(f"/api/v1/notices/{notice['id']}/ack", headers=h(other3.admin))).status_code == 404
    sent_rows = (await mfa_operator.client.get(f"{PLATFORM}/incidents/{incident.json()['id']}/notifications")).json()
    assert sent_rows[0]["acknowledged_count"] == 1
