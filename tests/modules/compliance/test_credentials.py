"""Credentials (Epic 21 A5): record, verify, renew (§5.7), unmask, Aadhaar last 4 + redaction review."""

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.compliance import Credential
from app.models.documents import DocumentVersion
from app.models.files import StoredFile
from tests.modules.compliance.helpers import CANDS, CREDS, PDF, credential, full_aadhaar, record, verify
from tests.modules.demands.helpers import DOrg, h
from tests.modules.documents.helpers import member_with
from tests.modules.offers.helpers import create_candidate

FUTURE = (date.today() + timedelta(days=400)).isoformat()


async def test_record_verify_renew_keeps_old_until_new_valid(dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Asha Passport")
    resp = await record(dorg, cand, "passport", number="k1234567", expires_on=FUTURE, issuer="RPO Bengaluru")
    assert resp.status_code == 201, resp.text
    c1 = resp.json()
    assert c1["number_last4"] == "4567" and c1["status"] == "pending_verification"
    assert c1["human_id"].startswith("CRD-") and "K1234567" not in resp.text
    assert (await verify(dorg, c1["id"])).json()["status"] == "valid"
    assert (await verify(dorg, c1["id"])).status_code == 409
    renewal = await dorg.admin.client.post(
        f"{CREDS}/{c1['id']}/renew",
        data={"expires_on": (date.today() + timedelta(days=3000)).isoformat()},
        files={"file": ("p.pdf", PDF, "application/pdf")},
        headers=h(dorg.admin),
    )
    assert renewal.status_code == 201, renewal.text
    c2 = renewal.json()
    assert c2["version_no"] == 2 and c2["previous_id"] == c1["id"] and c2["number_last4"] == "4567"
    assert (await credential(dorg, c1["id"])).superseded_at is None  # old stays effective
    assert (await verify(dorg, c2["id"])).status_code == 200
    assert (await credential(dorg, c1["id"])).superseded_at is not None
    listed = (await dorg.admin.client.get(f"{CANDS}/{cand}/credentials")).json()
    assert {c["status"] for c in listed} == {"valid", "superseded"}


async def test_validation_rules(dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Vik Validate")
    bad = await record(dorg, cand, "passport", number="12345678", expires_on=FUTURE)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_number_format"
    no_exp = await record(dorg, cand, "passport", number="K1234567")
    assert no_exp.json()["error"]["code"] == "expiry_required"
    no_doc = await record(dorg, cand, "passport", number="K1234567", expires_on=FUTURE, file=False)
    assert no_doc.json()["error"]["code"] == "document_required"
    note = await record(dorg, cand, "certification", expires_on=FUTURE, issuer=f"id {full_aadhaar()}")
    assert note.status_code == 422 and note.json()["error"]["code"] == "aadhaar_full_number_forbidden"
    assert (await record(dorg, cand, "aadhaar", number=full_aadhaar(), file=False)).status_code == 422
    unknown = await record(dorg, cand, "no_such_type", file=False)
    assert unknown.json()["error"]["code"] == "unknown_credential_type"


async def test_unmask_needs_identity_view_and_is_audited(app: FastAPI, dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Una Unmask")
    c = (await record(dorg, cand, "pan_card", number="ABCDE1234F")).json()
    reader = await member_with(app, dorg, "compliance:read")
    try:
        assert (await reader.client.post(f"{CREDS}/{c['id']}/unmask", headers=h(reader))).status_code == 403
    finally:
        await reader.client.aclose()
    ok = await dorg.admin.client.post(f"{CREDS}/{c['id']}/unmask", headers=h(dorg.admin))
    assert ok.status_code == 200 and ok.json()["number"] == "ABCDE1234F"
    assert ok.headers["cache-control"] == "no-store"
    async with tenant_session(dorg.tenant_id) as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "identity_unmasked"))).scalars().all()
        )
        assert any(str(r.entity_id) == c["id"] for r in rows)


async def test_aadhaar_last4_redaction_review_and_verify(dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Aadhya Aadhaar")
    resp = await record(dorg, cand, "aadhaar", number="0124")
    assert resp.status_code == 201, resp.text
    c = resp.json()
    assert c["number_last4"] == "0124" and c["redaction_status"] == "pending_review"
    row = await credential(dorg, c["id"])
    assert row.number is None and row.number_bidx is None
    early = await verify(dorg, c["id"])
    assert early.status_code == 409 and early.json()["error"]["code"] == "redaction_review_required"
    assert (
        await dorg.admin.client.post(f"{CREDS}/{c['id']}/unmask", headers=h(dorg.admin))
    ).status_code == 409
    doc = await dorg.admin.client.get(f"{CREDS}/{c['id']}/document")
    assert doc.status_code == 200 and doc.headers["cache-control"] == "private, no-store"
    ok = await dorg.admin.client.post(
        f"{CREDS}/{c['id']}/redaction", json={"decision": "confirmed"}, headers=h(dorg.admin)
    )
    assert ok.status_code == 200 and ok.json()["redaction_status"] == "confirmed"
    assert (await verify(dorg, c["id"])).json()["status"] == "valid"


async def test_rejected_redaction_deletes_the_object(dorg: DOrg) -> None:
    from app.core import storage

    cand = await create_candidate(dorg.admin, "Ravi Redact")
    c = (await record(dorg, cand, "aadhaar", number="0124")).json()
    async with tenant_session(dorg.tenant_id) as db:
        file_id = (
            await db.execute(
                select(DocumentVersion.file_id).where(
                    DocumentVersion.document_id == uuid.UUID(c["document_id"])
                )
            )
        ).scalar_one()
    resp = await dorg.admin.client.post(
        f"{CREDS}/{c['id']}/redaction", json={"decision": "rejected"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["redaction_status"] == "rejected"
    assert resp.json()["document_id"] is None
    async with tenant_session(dorg.tenant_id) as db:
        f = await db.get(StoredFile, file_id)
        assert f is not None and f.deleted_at is not None
        key = f.storage_key
    gone = False
    try:
        await storage.get_storage().get(key)
    except FileNotFoundError:
        gone = True
    assert gone


async def test_pending_aadhaar_image_and_verify_need_permissions(app: FastAPI, dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Ira Image")
    c = (await record(dorg, cand, "aadhaar", number="0124")).json()
    reader = await member_with(app, dorg, "compliance:read")
    try:
        assert (await reader.client.get(f"{CREDS}/{c['id']}/document")).status_code == 403
        attempt = await reader.client.post(
            f"{CREDS}/{c['id']}/verify", json={"decision": "valid"}, headers=h(reader)
        )
        assert attempt.status_code == 403
    finally:
        await reader.client.aclose()


async def test_numbers_absent_from_logs_lists_and_audit(dorg: DOrg, caplog: Any) -> None:
    cand = await create_candidate(dorg.admin, "Lia Logs")
    resp = await record(dorg, cand, "passport", number="Z7654321", expires_on=FUTURE)
    assert resp.status_code == 201
    listed = await dorg.admin.client.get(CREDS)
    assert "Z7654321" not in listed.text and "Z7654321" not in caplog.text
    async with tenant_session(dorg.tenant_id) as db:
        raw = (
            await db.execute(
                text("SELECT number FROM credentials WHERE id = :i"), {"i": uuid.UUID(resp.json()["id"])}
            )
        ).scalar()
        assert raw and "Z7654321" not in raw
        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.entity_type == "credential"))).scalars().all()
        )
        assert all("Z7654321" not in str(r.changes) for r in logs)


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, "Tara Tenant")
    c = (await record(dorg, cand, "pan_card", number="ABCDE1234F")).json()
    o = other_dorg.admin
    assert (await o.client.get(f"{CREDS}/{c['id']}")).status_code == 404
    assert (
        await o.client.post(f"{CREDS}/{c['id']}/verify", json={"decision": "valid"}, headers=h(o))
    ).status_code == 404
    assert (await o.client.post(f"{CREDS}/{c['id']}/unmask", headers=h(o))).status_code == 404
    red = await o.client.post(f"{CREDS}/{c['id']}/redaction", json={"decision": "confirmed"}, headers=h(o))
    assert red.status_code == 404
    assert (await o.client.get(f"{CREDS}/{c['id']}/document")).status_code == 404
    assert (await o.client.get(f"{CANDS}/{cand}/credentials")).status_code == 404
    assert (
        await o.client.post(f"{CREDS}/{c['id']}/renew", data={"issuer": "x"}, headers=h(o))
    ).status_code == 404
    async with tenant_session(other_dorg.tenant_id) as db:
        assert (
            await db.execute(select(Credential).where(Credential.id == uuid.UUID(c["id"])))
        ).first() is None
