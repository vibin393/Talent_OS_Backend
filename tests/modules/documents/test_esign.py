"""P2-9.3 / P2-9.4: ordered multi-party signing (Worker portal, internal session, secure link), OTP rules,
evidence, certificate, completion, verification, immutability, cancel, isolation."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core import storage
from app.core.database import tenant_session
from app.models.documents import DocumentVersion, Signature, SignatureEvent, SignatureRequest, SignerOtp
from app.models.files import StoredFile
from app.services.documents import esign, render
from app.services.documents import hooks as documents_hooks
from app.services.documents.common import now
from tests.epic3 import upload
from tests.factories import deferred_enqueues
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.documents.helpers import (
    DOCS,
    LINKS,
    WORK,
    candidate_with_email,
    emails,
    last_code,
    link_token,
    post_headers,
    published_template,
    send_request,
)
from tests.modules.identity.conftest import new_client


async def _two_party(dorg: DOrg) -> tuple[dict[str, Any], uuid.UUID, str]:
    template = await published_template(dorg.admin)
    cid, email = await candidate_with_email(dorg)
    req = await send_request(
        dorg, template["id"], cid, extra_signers=[{"kind": "user", "principal_id": str(dorg.admin.user_id)}]
    )
    return req, cid, email


async def _worker_signs(app: FastAPI, as_candidate: Any, dorg: DOrg, req: dict[str, Any], cid: uuid.UUID, email: str) -> None:
    as_candidate(dorg.tenant_id, cid)
    signer_id = req["signers"][0]["id"]
    async with new_client(app) as c:
        listed = (await c.get(WORK)).json()
        assert [x["signer_id"] for x in listed] == [signer_id] and listed[0]["your_turn"] is True
        assert (await c.get(f"{WORK}/{signer_id}/view")).headers["content-type"] == "application/pdf"
        no_consent = await c.post(f"{WORK}/{signer_id}/sign", json={"typed_name": "Asha Rao", "agreed": False})
        assert (no_consent.status_code, no_consent.json()["error"]["code"]) == (422, "consent_required")
        assert (await c.post(f"{WORK}/{signer_id}/consent", json={"agreed": True})).status_code == 200
        sent = await c.post(f"{WORK}/{signer_id}/otp")
        assert sent.status_code == 200 and sent.json()["email_hint"].endswith("@example.com")
        wrong_name = await c.post(
            f"{WORK}/{signer_id}/sign", json={"typed_name": "Someone Else", "otp": last_code(dorg.tenant_id, email)}
        )
        assert (wrong_name.status_code, wrong_name.json()["error"]["code"]) == (422, "name_mismatch")
        ok = await c.post(
            f"{WORK}/{signer_id}/sign", json={"typed_name": "asha  RAO", "otp": last_code(dorg.tenant_id, email)}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["request_status"] == "partially_signed"


async def test_two_party_flow_certificate_and_verify(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    req, cid, email = await _two_party(dorg)
    assert req["status"] == "sent" and req["doc_sha256"] and [s["order"] for s in req["signers"]] == [1, 2]
    # Signer 1 only is notified (secure link email); signer 2 is not yet.
    assert len(emails(dorg.tenant_id, "esign_requested", email)) == 1
    signatory = req["signers"][1]["id"]
    early = await dorg.admin.client.get(f"{DOCS}/signers/{signatory}")
    assert early.status_code == 200 and early.json()["your_turn"] is False
    not_yet = await dorg.admin.client.post(f"{DOCS}/signers/{signatory}/otp", headers=h(dorg.admin))
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (409, "not_your_turn")

    await _worker_signs(app, as_candidate, dorg, req, cid, email)

    m = dorg.admin
    me = (await m.client.get(f"/api/v1/admin/users/{m.user_id}")).json()
    assert (await m.client.post(f"{DOCS}/signers/{signatory}/otp", headers=h(m))).status_code == 200
    signed = await m.client.post(
        f"{DOCS}/signers/{signatory}/sign",
        json={"typed_name": me["name"], "otp": last_code(dorg.tenant_id, me["email"]), "agreed": True},
        headers=h(m),
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["request_status"] == "completed"

    detail = (await m.client.get(f"{DOCS}/signature-requests/{req['id']}")).json()
    assert detail["status"] == "completed" and detail["final_sha256"] and detail["final_version_id"]
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds[:2] == ["created", "sent"] and kinds.count("signed") == 2 and kinds[-1] == "completed"
    doc = (await m.client.get(f"{DOCS}/{detail['document_id']}")).json()
    assert doc["signed"] is True and doc["current_version"]["source"] == "signed"
    final = await m.client.get(doc["current_version"]["download_url"])
    assert final.status_code == 200
    text_ = render.pdf_text(final.content)
    flat = "".join(text_.split())
    assert "Signature certificate" in text_ and detail["doc_sha256"] in flat and str(detail["certificate_id"]) in flat
    assert "asha RAO" in text_ or "ashaRAO" in flat
    assert "verified" in text_ and "/verify?c=" in flat
    # Completion email with the final PDF to every signer.
    assert len(emails(dorg.tenant_id, "esign_completed")) == 2

    verify = await m.client.post(
        f"{DOCS}/verify", files={"file": ("x.pdf", final.content, "application/pdf")}, headers=h(m)
    )
    body = verify.json()
    assert body["match"] == "final" and len(body["signers"]) == 2 and all(s["signed_at"] for s in body["signers"])
    original = await m.client.get(f"{DOCS}/{doc['id']}/versions/{detail['document_version_id']}/download")
    again = await m.client.post(
        f"{DOCS}/verify", files={"file": ("x.pdf", original.content, "application/pdf")}, headers=h(m)
    )
    assert again.json()["match"] == "original"
    none = await m.client.post(f"{DOCS}/verify", files={"file": ("x.pdf", b"%PDF-1.4 nope", "application/pdf")}, headers=h(m))
    assert none.json()["match"] == "none"
    async with new_client(app) as anon:
        pub = (await anon.get(f"/api/v1/public/verify/{detail['certificate_id']}")).json()
        assert pub == {"valid": True, "title": detail["title"], "completed_at": pub["completed_at"], "signer_count": 2}
        assert (await anon.get(f"/api/v1/public/verify/{uuid.uuid4()}")).json()["valid"] is False

    # Completion is idempotent; signed documents refuse replacement (supersede instead).
    async with tenant_session(dorg.tenant_id) as db:
        again_file = await esign.complete_request(admin_ctx(dorg), db, uuid.UUID(req["id"]))
        v = await db.get(DocumentVersion, uuid.UUID(detail["final_version_id"]))
        assert v is not None and again_file == v.file_id
    replace = await upload(m.client, f"{DOCS}/{doc['id']}/versions", final.content, "x.pdf")
    assert (replace.status_code, replace.json()["error"]["code"]) == (409, "document_signed")


async def test_otp_attempts_lock_and_expiry(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    template = await published_template(dorg.admin)
    cid, email = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid)
    signer_id = req["signers"][0]["id"]
    as_candidate(dorg.tenant_id, cid)
    async with new_client(app) as c:
        await c.post(f"{WORK}/{signer_id}/otp")
        codes = []
        for _ in range(5):
            r = await c.post(f"{WORK}/{signer_id}/sign", json={"typed_name": "Asha Rao", "otp": "000000", "agreed": True})
            codes.append((r.status_code, r.json()["error"]["code"]))
        assert codes[:4] == [(422, "otp_invalid")] * 4 and codes[4] == (429, "otp_locked")
        locked = await c.post(
            f"{WORK}/{signer_id}/sign", json={"typed_name": "Asha Rao", "otp": last_code(dorg.tenant_id, email), "agreed": True}
        )
        assert (locked.status_code, locked.json()["error"]["code"]) == (429, "otp_locked")
        await c.post(f"{WORK}/{signer_id}/otp")
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(update(SignerOtp).where(SignerOtp.consumed_at.is_(None)).values(expires_at=now() - timedelta(minutes=1)))
        expired = await c.post(
            f"{WORK}/{signer_id}/sign", json={"typed_name": "Asha Rao", "otp": last_code(dorg.tenant_id, email), "agreed": True}
        )
        assert (expired.status_code, expired.json()["error"]["code"]) == (410, "otp_expired")
        for _ in range(3):
            await c.post(f"{WORK}/{signer_id}/otp")
        too_many = await c.post(f"{WORK}/{signer_id}/otp")
        assert (too_many.status_code, too_many.json()["error"]["code"]) == (429, "otp_rate_limited")
    async with tenant_session(dorg.tenant_id) as db:
        failed = (await db.execute(select(SignatureEvent).where(SignatureEvent.kind == "otp_failed"))).scalars().all()
        assert len(failed) == 5


async def test_secure_link_signing_scanner_get_and_race(app: FastAPI, dorg: DOrg) -> None:
    template = await published_template(dorg.admin)
    cid, email = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(SignatureRequest).where(SignatureRequest.id == uuid.UUID(req["id"])).values(require_otp=False))
    (msg,) = emails(dorg.tenant_id, "esign_requested", email)
    token = link_token(msg)
    async with new_client(app) as a, new_client(app) as b:
        # Mail-scanner GETs never change anything.
        for _ in range(2):
            page = await a.get(f"{LINKS}/{token}")
            assert page.status_code == 200 and page.json()["purpose"] == "esign"
            assert (await a.get(f"{LINKS}/{token}/esign")).json()["status"] == "sent"
            assert (await a.get(f"{LINKS}/{token}/esign/view")).status_code == 200
        no_origin = await a.post(f"{LINKS}/{token}/esign/sign", json={"typed_name": "Asha Rao", "agreed": True})
        assert no_origin.status_code == 403
        body = {"typed_name": "Asha Rao", "agreed": True}
        with deferred_enqueues(documents_hooks):
            results = await asyncio.gather(
                a.post(f"{LINKS}/{token}/esign/sign", json=body, headers=post_headers()),
                b.post(f"{LINKS}/{token}/esign/sign", json=body, headers=post_headers()),
            )
        codes = sorted(r.status_code for r in results)
        assert codes[0] == 200 and codes[1] in (409, 410), [r.text for r in results]
        assert (await a.get(f"{LINKS}/{token}")).status_code == 410
    async with tenant_session(dorg.tenant_id) as db:
        sigs = (await db.execute(select(Signature))).scalars().all()
        assert len(sigs) == 1 and sigs[0].channel == "secure_link" and sigs[0].otp_verified_at is None
        assert str(sigs[0].user_agent) == "pytest-browser/1.0"
        events = (await db.execute(select(SignatureEvent.data))).scalars().all()
        assert all(token not in str(d) for d in events)
        req_row = await db.get(SignatureRequest, uuid.UUID(req["id"]))
        assert req_row is not None and req_row.status == "completed"


async def test_hash_tamper_is_409(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    template = await published_template(dorg.admin)
    cid, _ = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(SignatureRequest).values(require_otp=False))
        v = await db.get(DocumentVersion, uuid.UUID(req["document_version_id"]))
        f = await db.get(StoredFile, v.file_id) if v else None
        assert f is not None
        await storage.get_storage().put(f.storage_key, b"%PDF-1.4 tampered", "application/pdf")
    as_candidate(dorg.tenant_id, cid)
    async with new_client(app) as c:
        signer_id = req["signers"][0]["id"]
        r = await c.post(f"{WORK}/{signer_id}/sign", json={"typed_name": "Asha Rao", "agreed": True})
        assert (r.status_code, r.json()["error"]["code"]) == (409, "document_hash_mismatch")


async def test_cancel_revokes_links_and_immutability(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    req, cid, email = await _two_party(dorg)
    token = link_token(emails(dorg.tenant_id, "esign_requested", email)[0])
    m = dorg.admin
    cancelled = await m.client.post(
        f"{DOCS}/signature-requests/{req['id']}/cancel", json={"reason": "Wrong template"}, headers=h(m)
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert all(s["status"] == "void" for s in cancelled.json()["signers"])
    async with new_client(app) as anon:
        assert (await anon.get(f"{LINKS}/{token}")).status_code == 410
    again = await m.client.post(f"{DOCS}/signature-requests/{req['id']}/cancel", json={"reason": "again"}, headers=h(m))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "invalid_transition")
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(
                update(SignatureRequest).where(SignatureRequest.id == uuid.UUID(req["id"])).values(doc_sha256="0" * 64)
            )


async def test_signature_rows_are_immutable_after_completion(dorg: DOrg, as_candidate: Any, app: FastAPI) -> None:
    template = await published_template(dorg.admin)
    cid, _ = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(SignatureRequest).values(require_otp=False))
    as_candidate(dorg.tenant_id, cid)
    async with new_client(app) as c:
        r = await c.post(f"{WORK}/{req['signers'][0]['id']}/sign", json={"typed_name": "Asha Rao", "agreed": True})
        assert r.status_code == 200 and r.json()["request_status"] == "completed"
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(update(Signature).values(typed_name="forged"))
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(text("DELETE FROM signature_events"))


async def test_isolation_cross_tenant_and_other_worker(
    app: FastAPI, dorg: DOrg, other_dorg: DOrg, as_candidate: Any
) -> None:
    req, cid, _ = await _two_party(dorg)
    other = other_dorg.admin
    for url in (
        f"{DOCS}/signature-requests/{req['id']}",
        f"{DOCS}/{req['document_id']}",
        f"{DOCS}/signers/{req['signers'][1]['id']}",
    ):
        assert (await other.client.get(url)).status_code == 404, url
    stranger, _ = await candidate_with_email(dorg, "Ravi Kumar")
    as_candidate(dorg.tenant_id, stranger)
    async with new_client(app) as c:
        assert (await c.get(WORK)).json() == []
        assert (await c.get(f"{WORK}/{req['signers'][0]['id']}")).status_code == 404
        r = await c.post(f"{WORK}/{req['signers'][0]['id']}/sign", json={"typed_name": "Asha Rao", "agreed": True})
        assert r.status_code == 404
    as_candidate(other_dorg.tenant_id, cid)
    async with new_client(app) as c:
        assert (await c.get(f"{WORK}/{req['signers'][0]['id']}")).status_code == 404


async def test_decline_notifies_owner(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    req, cid, _ = await _two_party(dorg)
    as_candidate(dorg.tenant_id, cid)
    async with new_client(app) as c:
        r = await c.post(f"{WORK}/{req['signers'][0]['id']}/decline", json={"reason": "Not now"})
        assert r.status_code == 200 and r.json()["request_status"] == "declined"
    notes = (await dorg.admin.client.get("/api/v1/notifications")).json()
    items = notes["items"] if isinstance(notes, dict) else notes
    assert any(n["type"] == "esign_declined" for n in items)


async def test_expiry_beat(dorg: DOrg) -> None:
    template = await published_template(dorg.admin)
    cid, _ = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(SignatureRequest).values(expires_at=now() - timedelta(minutes=1)))
    async with tenant_session(dorg.tenant_id) as db:
        assert await esign.expire_due(admin_ctx(dorg), db) == 1
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(SignatureRequest, uuid.UUID(req["id"]))
        assert row is not None and row.status == "expired"
