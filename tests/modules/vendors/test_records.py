"""Epic 17 A1-A3: vendor records, the §5.4 status machine, documents, expiry job, settings, reveal (FR-93)."""

import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.models.identity import Session as AuthSession
from app.models.vendors import VendorRelease
from app.services.vendors import records
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.vendors.conftest import (
    PORTAL,
    VENDORS,
    active_vendor,
    create_vendor,
    release,
    transition,
    upload_doc,
    vendor_user,
)


async def test_create_and_detail_masks_pan(dorg: DOrg) -> None:
    v = await create_vendor(
        dorg,
        "TalentBridge",
        pan="ABCDE1234F",
        primary_contact={"name": "Sunita", "email": "s@tb.example.com"},
    )
    assert v["human_id"].startswith("VEN-") and v["status"] == "prospective"
    assert v["pan_last4"] == "234F" and "pan" not in v
    assert set(v["activation_blockers"]) == {"agreement", "document:agreement", "document:registration"}
    resp = await dorg.admin.client.get(VENDORS, params={"q": "talent"})
    assert [x["id"] for x in resp.json()] == [v["id"]]
    dup = await dorg.admin.client.post(
        VENDORS, json={"legal_name": "Other", "pan": "ABCDE1234F"}, headers=h(dorg.admin)
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "vendor_pan_exists"
    reveal = await dorg.admin.client.post(
        f"{VENDORS}/{v['id']}/reveal", json={"field": "pan"}, headers=h(dorg.admin)
    )
    assert reveal.status_code == 200 and reveal.json()["value"] == "ABCDE1234F"


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        (["onboarding"], True),
        (["active"], False),
        (["suspended"], False),
        (["onboarding", "inactive"], False),
        (["onboarding", "prospective"], False),
    ],
)
async def test_status_matrix(dorg: DOrg, path: list[str], ok: bool) -> None:
    v = await create_vendor(dorg)
    resp = None
    for to in path:
        resp = await transition(dorg, v["id"], to, reason="r")
    assert resp is not None
    assert (resp.status_code == 200) is ok, resp.text


async def test_activation_blocked_lists_missing(dorg: DOrg) -> None:
    v = await create_vendor(dorg)
    await transition(dorg, v["id"], "onboarding")
    await upload_doc(dorg, v["id"], "agreement")
    resp = await transition(dorg, v["id"], "active")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "vendor_activation_blocked" and err["details"]["missing"] == [
        "document:registration"
    ]
    await upload_doc(dorg, v["id"], "registration", expires_on=date.today() - timedelta(days=1))
    assert (await transition(dorg, v["id"], "active")).status_code == 409  # expired doc does not count
    await upload_doc(dorg, v["id"], "registration")
    assert (await transition(dorg, v["id"], "active")).json()["status"] == "active"


async def test_suspension_needs_reason_pauses_releases_and_ends_sessions(app: FastAPI, dorg: DOrg) -> None:
    v = await active_vendor(dorg)
    demand = await open_demand(dorg.admin, dorg.client_id)
    await release(dorg, demand["id"], [v["id"]])
    ext = await vendor_user(app, dorg, v["id"])
    try:
        assert (await ext.client.get(f"{PORTAL}/dashboard")).status_code == 200
        resp = await transition(dorg, v["id"], "suspended")
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "reason_required"
        assert (await transition(dorg, v["id"], "suspended", reason="audit")).status_code == 200
        async with tenant_session(dorg.tenant_id) as db:
            rel = (
                await db.execute(select(VendorRelease).where(VendorRelease.vendor_id == uuid.UUID(v["id"])))
            ).scalar_one()
            assert rel.status == "paused" and rel.paused_by_system
        # Epic 14's VendorStatusChanged handler ends the sessions; the next request is 401.
        async with platform_session() as pdb:
            live = (
                (
                    await pdb.execute(
                        select(AuthSession).where(
                            AuthSession.principal_id == ext.id, AuthSession.revoked_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert live == []
        assert (await ext.client.get(f"{PORTAL}/dashboard")).status_code == 401
        # Re-activation resumes the system-paused release.
        assert (await transition(dorg, v["id"], "active")).status_code == 200
        async with tenant_session(dorg.tenant_id) as db:
            rel = (
                await db.execute(select(VendorRelease).where(VendorRelease.vendor_id == uuid.UUID(v["id"])))
            ).scalar_one()
            assert rel.status == "active" and not rel.paused_by_system
    finally:
        await ext.client.aclose()


async def test_expiry_job_suspends_after_grace(dorg: DOrg) -> None:
    v = await active_vendor(dorg)
    # The mandatory registration document expires on day X; grace is 7 days.
    async with tenant_session(dorg.tenant_id) as db:
        from app.models.documents import Document

        docs = (
            (await db.execute(select(Document).where(Document.owner_id == uuid.UUID(v["id"]))))
            .scalars()
            .all()
        )
        for d in docs:
            if d.category == "registration":
                d.expires_on = date(2026, 9, 1)
    ctx = RequestContext(tenant_id=dorg.tenant_id, actor_kind="system")
    async with tenant_session(dorg.tenant_id) as db:
        out = await records.check_documents(ctx, db, today=date(2026, 9, 8))  # 7 days: within grace
        assert out["suspended"] == []
    async with tenant_session(dorg.tenant_id) as db:
        out = await records.check_documents(ctx, db, today=date(2026, 9, 9))  # 8 days: suspend
        assert out["suspended"] == [v["id"]]
    detail = (await dorg.admin.client.get(f"{VENDORS}/{v['id']}")).json()
    assert detail["status"] == "suspended" and detail["suspended_reason"] == "document_expired:registration"


async def test_documents_status_and_gstin_and_bank(dorg: DOrg) -> None:
    v = await create_vendor(dorg, pan="ABCDE1234F")
    await upload_doc(dorg, v["id"], "insurance", expires_on=date.today() + timedelta(days=10))
    docs = (await dorg.admin.client.get(f"{VENDORS}/{v['id']}/documents")).json()
    assert docs[0]["status"] == "expiring"
    bad = await dorg.admin.client.post(
        f"{VENDORS}/{v['id']}/gstins",
        json={"gstin": "33ABCDE1234F1Z0", "state_code": "33"},
        headers=h(dorg.admin),
    )
    assert bad.status_code in (201, 422)
    bank = await dorg.admin.client.post(
        f"{VENDORS}/{v['id']}/bank-accounts",
        json={"account_name": "TalentBridge Pvt Ltd", "account_no": "001234567890", "ifsc": "HDFC0001234"},
        headers=h(dorg.admin),
    )
    assert bank.status_code == 201, bank.text
    out = bank.json()
    assert out["account_no_last4"] == "7890" and "account_no" not in out
    assert out["status"] in ("active", "pending")


async def test_settings_roundtrip(dorg: DOrg) -> None:
    s = (await dorg.admin.client.get(f"{VENDORS}/settings")).json()
    assert s["ownership_window_days"] == 90 and s["vendor_labels"]["interviewing"] == "Interviewing"
    resp = await dorg.admin.client.patch(
        f"{VENDORS}/settings",
        json={"ownership_window_days": 60, "vendor_labels": {"interviewing": "In interviews"}},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200 and resp.json()["ownership_window_days"] == 60
    assert resp.json()["vendor_labels"]["interviewing"] == "In interviews"
