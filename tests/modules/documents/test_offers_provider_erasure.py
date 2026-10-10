"""P2-9.5: P1 offer acceptance on the signature tables (with / without OTP), provider port swap, erasure."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.documents import SignatureRequestCompleted, SignatureRequestStatusChanged
from app.models.connectors import ProviderConnectorConfig
from app.models.documents import Document, Signature, SignatureRequest, Signer
from app.models.files import StoredFile
from app.models.offers import Offer, OfferSettings
from app.services.connectors.esign.fake import FakeEsignProvider
from app.services.documents import esign
from app.services.documents.render import pdf_text
from app.services.privacy.registry import ErasureContext
from tests.modules.demands.helpers import DOrg, admin_ctx
from tests.modules.documents.helpers import (
    LINKS,
    WORK,
    candidate_with_email,
    emails,
    last_code,
    post_headers,
    published_template,
    send_request,
    upload_doc,
)
from tests.modules.identity.conftest import new_client
from tests.modules.offers.helpers import get_offer, link_token, released_offer, setup_selected


async def _offer_token(dorg: DOrg) -> str:
    (message,) = emails(dorg.tenant_id, "offer_released")[-1:]
    return link_token(message)


async def test_offer_acceptance_without_otp_uses_signature_tables(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    token = await _offer_token(dorg)
    async with new_client(app) as anon:
        assert (await anon.get(f"{LINKS}/{token}/offer-otp")).json() == {"required": False}
        ok = await anon.post(
            f"{LINKS}/{token}/accept", json={"typed_name": "Asha Rao", "version_no": 1, "agreed": True}, headers=post_headers()
        )
        assert ok.status_code == 200, ok.text
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(Offer, uuid.UUID(offer["id"]))
        assert row is not None and row.signature_request_id is not None
        req = await db.get(SignatureRequest, row.signature_request_id)
        assert req is not None and req.status == "completed" and req.subject_kind == "offer"
        assert req.final_version_id is not None
        (sig,) = (await db.execute(select(Signature).where(Signature.request_id == req.id))).scalars().all()
        assert sig.channel == "secure_link" and sig.otp_verified_at is None and sig.typed_name == "Asha Rao"
        final = await db.get(StoredFile, row.accepted_file_id)
        assert final is not None and final.sha256 == req.final_sha256
    signed = emails(dorg.tenant_id, "offer_accepted")
    text = pdf_text(signed[0].attachments[0].data)
    assert "Electronic acceptance certificate" in text and "email code not used" in text


async def test_offer_acceptance_with_otp(app: FastAPI, dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        db.add(OfferSettings(tenant_id=dorg.tenant_id, acceptance_requires_otp=True))
    s = await setup_selected(dorg, email="asha.otp@example.com")
    offer = await released_offer(s)
    token = await _offer_token(dorg)
    body = {"typed_name": "Asha Rao", "version_no": 1, "agreed": True}
    async with new_client(app) as anon:
        assert (await anon.get(f"{LINKS}/{token}/offer-otp")).json() == {"required": True}
        no_code = await anon.post(f"{LINKS}/{token}/accept", json=body, headers=post_headers())
        assert (no_code.status_code, no_code.json()["error"]["code"]) == (422, "otp_invalid")
        sent = await anon.post(f"{LINKS}/{token}/offer-otp", headers=post_headers())
        assert sent.status_code == 200, sent.text
        ok = await anon.post(
            f"{LINKS}/{token}/accept", json={**body, "otp": last_code(dorg.tenant_id, "asha.otp@example.com")},
            headers=post_headers(),
        )  # fmt: skip
        assert ok.status_code == 200, ok.text
    assert (await get_offer(dorg.admin, offer["id"]))["status"] == "accepted"
    async with tenant_session(dorg.tenant_id) as db:
        (sig,) = (await db.execute(select(Signature))).scalars().all()
        assert sig.otp_verified_at is not None


async def test_offer_decline_declines_the_request(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    await released_offer(s)
    token = await _offer_token(dorg)
    async with new_client(app) as anon:
        await anon.get(f"{LINKS}/{token}/offer-otp")
        sent = await anon.post(f"{LINKS}/{token}/offer-otp", headers=post_headers())
        assert sent.status_code == 200
        r = await anon.post(f"{LINKS}/{token}/decline", json={"version_no": 1}, headers=post_headers())
        assert r.status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        (req,) = (await db.execute(select(SignatureRequest))).scalars().all()
        assert req.status == "declined"


async def test_provider_swap_same_events(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    seen: list[tuple[str, str]] = []

    def on_status(e: SignatureRequestStatusChanged) -> None:
        seen.append(("status", e.to_status))

    def on_done(e: SignatureRequestCompleted) -> None:
        seen.append(("completed", e.subject_kind))

    subscribe(SignatureRequestStatusChanged, on_status)
    subscribe(SignatureRequestCompleted, on_done)
    try:
        template = await published_template(dorg.admin)
        cid, _ = await candidate_with_email(dorg)
        builtin = await send_request(dorg, template["id"], cid)
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(update(SignatureRequest).values(require_otp=False))
        as_candidate(dorg.tenant_id, cid)
        async with new_client(app) as c:
            r = await c.post(f"{WORK}/{builtin['signers'][0]['id']}/sign", json={"typed_name": "Asha Rao", "agreed": True})
            assert r.status_code == 200
        builtin_seen = list(seen)
        seen.clear()

        FakeEsignProvider.reset()
        async with tenant_session(dorg.tenant_id) as db:
            db.add(ProviderConnectorConfig(
                tenant_id=dorg.tenant_id, category="esign", provider_key="fake", display_name="Fake", settings={},
                status="active",
            ))  # fmt: skip
        fake = await send_request(dorg, template["id"], cid)
        assert fake["provider"] == "fake"
        assert [c[0] for c in FakeEsignProvider.calls] == ["create_request"]
        async with tenant_session(dorg.tenant_id) as db:
            ref = (await db.get(SignatureRequest, uuid.UUID(fake["id"]))).external_ref  # type: ignore[union-attr]
        FakeEsignProvider.statuses[str(ref)] = "completed"
        async with tenant_session(dorg.tenant_id) as db:
            assert await esign.apply_provider_status(admin_ctx(dorg), db, uuid.UUID(fake["id"])) == "completed"
        assert seen == builtin_seen
        assert builtin_seen == [("status", "sent"), ("status", "completed"), ("completed", "candidate")]
    finally:
        unsubscribe(SignatureRequestStatusChanged, on_status)
        unsubscribe(SignatureRequestCompleted, on_done)
        FakeEsignProvider.reset()


async def test_erasure_retains_signed_and_erases_unsigned(app: FastAPI, dorg: DOrg, as_candidate: Any) -> None:
    from app.services.documents_erasure import DocumentsHandler

    template = await published_template(dorg.admin)
    cid, _ = await candidate_with_email(dorg)
    unsigned = await upload_doc(dorg, cid, category="other", title="Notes about Asha")
    signed_req = await send_request(dorg, template["id"], cid)
    pending_req = await send_request(dorg, template["id"], cid)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(SignatureRequest).values(require_otp=False))
    as_candidate(dorg.tenant_id, cid)
    async with new_client(app) as c:
        r = await c.post(f"{WORK}/{signed_req['signers'][0]['id']}/sign", json={"typed_name": "Asha Rao", "agreed": True})
        assert r.status_code == 200
    handler = DocumentsHandler()
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported is not None and len(exported["signatures"]) == 1
        counts = await handler.erase(db, ctx)
        assert counts["retained"] >= 2 and counts["files_deleted"] >= 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
        doc = await db.get(Document, uuid.UUID(unsigned["id"]))
        assert doc is not None and doc.title == "[erased]"
        kept = await db.get(SignatureRequest, uuid.UUID(signed_req["id"]))
        assert kept is not None and kept.status == "completed"
        (sig,) = (await db.execute(select(Signature).where(Signature.request_id == kept.id))).scalars().all()
        assert sig.typed_name == "Asha Rao"
        gone = await db.get(SignatureRequest, uuid.UUID(pending_req["id"]))
        assert gone is not None and gone.status == "cancelled"
        names = (await db.execute(select(Signer.name).where(Signer.request_id == gone.id))).scalars().all()
        assert names == ["[erased]"]
