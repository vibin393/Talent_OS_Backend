"""Worker profile / PAN / bank (encrypted, unmask audited), the Worker portal and erasure."""

import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.security import CSRF_HEADER
from app.models.placements import WorkerBankAccount, WorkerProfile
from app.schemas.placements_portal import PortalPlacementOut
from app.services.placements_erasure import PlacementsHandler
from app.services.privacy.registry import ErasureContext
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join
from tests.modules.placements.helpers import PLACEMENTS, WORKERS, direct_placement
from tests.p2 import sign_in_candidate

PAN = "ABCPE1234F"
ACCOUNT = "123456789012"
BANK = {
    "holder_name": "Asha Rao",
    "account_number": ACCOUNT,
    "account_number_confirm": ACCOUNT,
    "ifsc": "HDFC0001234",
}
PORTAL = "/api/v1/portal/work"
CSRF = {CSRF_HEADER: "csrf-token"}


def test_worker_schema_has_no_commercial_fields() -> None:
    fields = set(PortalPlacementOut.model_fields)
    assert not {f for f in fields if f.startswith(("bill_rate", "vendor_rate", "margin", "burden"))}


async def test_pan_and_bank_validation_unmask_and_audit(dorg: DOrg, caplog: pytest.LogCaptureFixture) -> None:
    body = await direct_placement(dorg)
    cid = body["candidate_id"]
    m = dorg.admin
    bad = await m.client.post(f"{WORKERS}/{cid}/pan", json={"pan": "ABC123"}, headers=h(m))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_pan"
    caplog.set_level(logging.DEBUG)
    ok = await m.client.post(f"{WORKERS}/{cid}/pan", json={"pan": PAN, "pan_name": "Asha Rao"}, headers=h(m))
    assert ok.status_code == 200 and ok.json()["pan_last4"] == "234F"
    bad_ifsc = await m.client.post(
        f"{WORKERS}/{cid}/bank-accounts", json={**BANK, "ifsc": "HDFC1234567"}, headers=h(m)
    )
    assert bad_ifsc.status_code == 422 and bad_ifsc.json()["error"]["code"] == "invalid_ifsc"
    mismatch = await m.client.post(
        f"{WORKERS}/{cid}/bank-accounts", json={**BANK, "account_number_confirm": "1"}, headers=h(m)
    )
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "account_mismatch"
    worker = (await m.client.get(f"{WORKERS}/{cid}")).json()
    assert worker["profile"]["pan_last4"] == "234F"
    assert PAN not in str(worker)
    # Tenant Admin holds identity:view -> unmask, no-store, audited
    resp = await m.client.post(f"{WORKERS}/{cid}/unmask", json={"field": "pan"}, headers=h(m))
    assert resp.status_code == 200 and resp.json()["value"] == PAN
    assert resp.headers["cache-control"] == "no-store"
    async with tenant_session(dorg.tenant_id) as db:
        n = (
            await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'identity_unmasked'"))
        ).scalar_one()
    assert n >= 1
    assert PAN not in caplog.text and ACCOUNT not in caplog.text
    async with tenant_session(dorg.tenant_id) as db:
        stored = (
            await db.execute(
                text("SELECT pan FROM worker_profiles WHERE candidate_id = :c"), {"c": uuid.UUID(cid)}
            )
        ).scalar_one()
    assert stored and PAN not in stored  # encrypted at rest


async def test_unmask_requires_identity_view(app: FastAPI, dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    from app.core.context import RequestContext
    from app.core.permissions import CATALOG, Principal, Scope
    from app.services.placements import workers

    perms = {k: Scope.TENANT for k in CATALOG if k != "identity:view"}
    ctx = RequestContext(
        tenant_id=dorg.tenant_id,
        principal=Principal(type="user", id=dorg.admin.user_id, tenant_id=dorg.tenant_id, permissions=perms),
    )
    async with tenant_session(dorg.tenant_id) as db:
        await workers.record_pan(ctx, db, uuid.UUID(body["candidate_id"]), PAN)
    from app.core.errors import Forbidden

    with pytest.raises(Forbidden):
        async with tenant_session(dorg.tenant_id) as db:
            await workers.unmask_field(ctx, db, uuid.UUID(body["candidate_id"]), "pan", None)


async def test_portal_bank_change_needs_hr_approval_and_notifies_worker(app: FastAPI, dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    cid = uuid.UUID(body["candidate_id"])
    hr = await join(app, dorg.tenant, "hr_onboarding", name="Hari HR")
    worker = await sign_in_candidate(app, cid, dorg.tenant_id)
    try:
        mine = await worker.get(f"{PORTAL}/placements")
        assert mine.status_code == 200, mine.text
        assert [p["id"] for p in mine.json()] == [body["id"]]
        assert "bill_rate" not in mine.text and "margin" not in mine.text
        resp = await worker.post(f"{PORTAL}/bank-accounts", json=BANK, headers=CSRF)
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "pending_approval" and resp.json()["account_number_last4"] == "9012"
        async with tenant_session(dorg.tenant_id) as db:
            row = (
                await db.execute(select(WorkerBankAccount).where(WorkerBankAccount.candidate_id == cid))
            ).scalar_one()
            notes = (
                (
                    await db.execute(
                        text(
                            "SELECT title FROM notifications WHERE recipient_kind = 'candidate' AND recipient_id = :c"
                        ),
                        {"c": cid},
                    )
                )
                .scalars()
                .all()
            )
        assert any("9012" in t for t in notes) and not any(ACCOUNT in t for t in notes)
        decided = await hr.client.post(
            f"/api/v1/approvals/{row.approval_request_id}/decide", json={"action": "approve"}, headers=h(hr)
        )
        assert decided.status_code == 200, decided.text
        banks = (await worker.get(f"{PORTAL}/bank-accounts")).json()
        assert banks[0]["status"] == "active"
        w360 = (await dorg.admin.client.get(f"{WORKERS}/{cid}")).json()
        assert w360["bank_accounts"][0]["account_number_last4"] == "9012"
        # confirm joining from the portal (actor candidate)
        conf = await worker.post(f"{PORTAL}/placements/{body['id']}/confirm-joining", headers=CSRF)
        assert conf.status_code == 200 and conf.json()["status"] == "joining_confirmed"
        prof = await worker.patch(
            f"{PORTAL}/profile",
            json={"emergency_name": "Ravi Rao", "emergency_phone": "+91 98765 00000"},
            headers=CSRF,
        )
        assert prof.status_code == 200 and prof.json()["emergency_name"] == "Ravi Rao"
    finally:
        await worker.aclose()
        await hr.client.aclose()


async def test_portal_cross_worker_404(app: FastAPI, dorg: DOrg) -> None:
    a = await direct_placement(dorg)
    b = await direct_placement(dorg)
    worker = await sign_in_candidate(app, uuid.UUID(a["candidate_id"]), dorg.tenant_id)
    try:
        assert (await worker.get(f"{PORTAL}/placements/{b['id']}")).status_code == 404
        assert (
            await worker.post(f"{PORTAL}/placements/{b['id']}/confirm-joining", headers=CSRF)
        ).status_code == 404
        assert (await worker.get(f"{PORTAL}/placements/{a['id']}")).status_code == 200
        # internal routes are refused to the candidate principal
        assert (await worker.get(f"{PLACEMENTS}/{a['id']}")).status_code in (401, 403)
    finally:
        await worker.aclose()


async def test_erasure_keeps_facts_and_honours_retention(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    cid = uuid.UUID(body["candidate_id"])
    m = dorg.admin
    await m.client.post(f"{WORKERS}/{cid}/pan", json={"pan": PAN}, headers=h(m))
    await m.client.patch(f"{WORKERS}/{cid}/profile", json={"emergency_name": "Ravi"}, headers=h(m))
    await m.client.post(
        f"{PLACEMENTS}/{body['id']}/counter-offer", json={"note": "Personal note"}, headers=h(m)
    )
    handler = PlacementsHandler()
    async with tenant_session(dorg.tenant_id) as db:
        prof_id = (
            await db.execute(select(WorkerProfile.id).where(WorkerProfile.candidate_id == cid))
        ).scalar_one()
    retained: dict[str, Any] = {"worker_profiles": {prof_id}}
    ctx = ErasureContext(
        tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr", retained=retained
    )
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported is not None and exported["worker"]["pan_last4"] == "234F"
        counts = await handler.erase(db, ctx)
        assert counts["retained"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
        prof = (await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == cid))).scalar_one()
        assert prof.emergency_name is None and prof.pan is not None  # retained under worker_profile
        note = (
            await db.execute(
                text("SELECT counter_offer_note, human_id FROM placements WHERE id = :i"),
                {"i": uuid.UUID(body["id"])},
            )
        ).one()
        assert note.counter_offer_note == "[erased]" and note.human_id == body["human_id"]
    ctx2 = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        await handler.erase(db, ctx2)
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(WorkerProfile.pan).where(WorkerProfile.candidate_id == cid))
        ).scalar_one() is None
