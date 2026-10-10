"""Epic 35 story 35.6 (FR-184): External-Principal delivery (portal deep links, NFR-24 redaction, email opt-out)
and the portal inboxes mounted under the Client Portal, Vendor Portal and Worker "My Work"."""

import dataclasses
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.core.security import CSRF_HEADER
from app.models.notifications import Notification
from app.services import notifications_service as ns
from app.services.notifications_service import Content, Recipient
from tests.modules.demands.helpers import DOrg, create_client
from tests.p2 import make_client_user, make_vendor, make_vendor_user, sign_in_external


async def _deliver(
    tenant_id: uuid.UUID,
    recipients: list[Recipient],
    title: str,
    type_key: str = "workflow_notice",
    **kw: object,
) -> None:
    async with tenant_session(tenant_id) as db:
        await ns.deliver(db, type_key, recipients, Content(title=title, **kw), event_id=uuid.uuid4().hex)  # type: ignore[arg-type]


def test_external_type_without_portal_path_is_refused() -> None:
    base = ns.NOTIFICATION_TYPES["workflow_notice"]
    bad = dataclasses.replace(base, external=True, portal_path=None)
    with pytest.raises(ValidationFailed) as exc:
        ns.register_notification_type("e35_no_path", bad)
    assert exc.value.code == "portal_path_required"
    assert "e35_no_path" not in ns.NOTIFICATION_TYPES


def test_portal_link_never_internal() -> None:
    assert ns.portal_link("client_user", "approvals/1") == "/client/approvals/1"
    assert ns.portal_link("vendor_user", "/submissions/2") == "/vendor/submissions/2"
    assert ns.portal_link("candidate", "timesheets") == "/portal/work/timesheets"
    assert ns.portal_link("client_user", "/vendor/x") is None
    assert ns.portal_link("user", "/demands/1") is None


async def test_inbox_isolation_read_state_and_preferences(app: FastAPI, dorg: DOrg) -> None:
    tid = dorg.tenant_id
    other_client = await create_client(dorg.admin)
    cu_a = await make_client_user(tid, dorg.client_id)
    cu_b = await make_client_user(tid, other_client)
    definition = dataclasses.replace(
        ns.NOTIFICATION_TYPES["workflow_notice"], external=True, portal_path="/approvals/{approval_id}",
        mandatory_in_app=False,
    )  # fmt: skip
    with ns._types.override("e35_approval", definition):
        async with tenant_session(tid) as db:
            await ns.deliver(
                db, "e35_approval", [Recipient("client_user", cu_a), Recipient("client_user", cu_b)],
                Content(title="Approve timesheet", body="PAN ABCPE1234F on file", link_path="/timesheets/1"),
                event_id=uuid.uuid4().hex, payload={"approval_id": "abc"},
            )  # fmt: skip
        async with tenant_session(tid) as db:
            rows = list(
                (await db.execute(select(Notification).where(Notification.type == "e35_approval"))).scalars()
            )
        assert {r.link_path for r in rows} == {"/client/approvals/abc"}
        assert all("ABCPE1234F" not in (r.body or "") for r in rows)
        theirs = next(r.id for r in rows if r.recipient_id == cu_b)

        a = await sign_in_external(app, "client_user", cu_a, tid, dorg.client_id)
        base = "/api/v1/client-portal/notifications"
        inbox = (await a.client.get(base)).json()
        assert inbox["unread_count"] == 1 and len(inbox["items"]) == 1
        mine = inbox["items"][0]
        assert mine["link"] == "/client/approvals/abc"
        hdr = {CSRF_HEADER: a.csrf}
        assert (await a.client.post(f"{base}/{theirs}/read", headers=hdr)).status_code == 404
        read = await a.client.post(f"{base}/{mine['id']}/read", headers=hdr)
        assert read.status_code == 200 and read.json()["read_at"]
        assert (await a.client.post(f"{base}/read-all", headers=hdr)).json()["unread_count"] == 0

        prefs = (await a.client.get(f"{base}/preferences")).json()
        assert any(p["type"] == "e35_approval" and p["email"] is True for p in prefs)
        off = await a.client.patch(
            f"{base}/preferences", json={"type": "e35_approval", "email": False}, headers=hdr
        )
        assert off.status_code == 200, off.text
        assert next(p for p in off.json() if p["type"] == "e35_approval")["email"] is False


async def test_vendor_inbox_and_cross_portal(app: FastAPI, dorg: DOrg) -> None:
    tid = dorg.tenant_id
    vendor = await make_vendor(tid)
    other_vendor = await make_vendor(tid)
    vu = await make_vendor_user(tid, vendor)
    vu_other = await make_vendor_user(tid, other_vendor)
    await _deliver(tid, [Recipient("vendor_user", vu_other)], "Other vendor note")
    await _deliver(tid, [Recipient("vendor_user", vu)], "Your note")
    v = await sign_in_external(app, "vendor_user", vu, tid, vendor)
    inbox = (await v.client.get("/api/v1/vendor-portal/notifications")).json()
    assert [i["title"] for i in inbox["items"]] == ["Your note"]
    async with tenant_session(tid) as db:
        other_id = (
            await db.execute(select(Notification.id).where(Notification.recipient_id == vu_other))
        ).scalar_one()
    resp = await v.client.post(
        f"/api/v1/vendor-portal/notifications/{other_id}/read", headers={CSRF_HEADER: v.csrf}
    )
    assert resp.status_code == 404
    # a vendor session never reaches the client inbox
    assert (await v.client.get("/api/v1/client-portal/notifications")).status_code in (401, 403)
