# ruff: noqa: E501
"""Epic 51 (FR-253): DSC-signed invoice / credit-note PDFs.

Fake adapter end to end, Manual adapter (pending queue, delivery gate, stored render download, refused / accepted
uploads signed in the test with a throw-away certificate -- pyHanko signing is used ONLY here), immutability and
period-guard allow-list, credit notes, idempotency and retry."""

import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.models.billing import CreditNote, DscSignRequest, Invoice
from app.models.connectors import ProviderConnectorConfig
from app.models.documents import Document, DocumentVersion
from app.schemas.billing import CreditNoteIn, ManualInvoiceIn, ManualLineIn
from app.services.billing import dsc, issue, notes, portal
from app.services.billing import invoices as inv_service
from app.services.connectors.dsc.fake import MARKER, FakeDscSigner
from tests.modules.billing.conftest import API, BOrg, err, ok

D = Decimal


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(dsc, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    FakeDscSigner.reset()
    yield
    FakeDscSigner.reset()


# ---------------------------------------------------------------- helpers
async def _policy(org: BOrg, client_id: uuid.UUID | None = None, **body: Any) -> dict[str, Any]:
    payload = {
        "gst_registration_id": str(org.registration_id),
        "client_id": str(client_id or org.client_id),
        **body,
    }
    return dict(await ok(await org.c.post(f"{API}/dsc/policies", json=payload, headers=org.h()), 201))


async def _fake_connector(org: BOrg) -> None:
    async with tenant_session(org.tenant_id) as db:
        db.add(
            ProviderConnectorConfig(
                tenant_id=org.tenant_id,
                category="dsc",
                provider_key="fake",
                display_name="Fake DSC",
                settings={},
                status="active",
            )
        )


async def _issued_invoice(org: BOrg, amount: str = "1000.00") -> uuid.UUID:
    async with tenant_session(org.tenant_id) as db:
        out = await inv_service.create_manual(
            org.ctx(),
            db,
            ManualInvoiceIn(
                client_id=org.client_id,
                legal_entity_id=org.entity_id,
                lines=[ManualLineIn(description="Fee", rate=D(amount))],
            ),
        )
        inv_id = out.id
    async with tenant_session(org.tenant_id) as db:
        await issue.issue_invoice(org.ctx(), db, inv_id)
    return inv_id  # after commit: InvoiceIssued -> finance_p3.dsc_sign ran eagerly


async def _doc(org: BOrg, inv_id: uuid.UUID, model: Any = Invoice) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(select(model).where(model.id == inv_id))).scalar_one()


async def _set_irn_registered(org: BOrg, table: str, doc_id: uuid.UUID) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(f"UPDATE {table} SET einvoice_status = 'irn_registered', irn = :irn WHERE id = :i"),
            {"irn": "b" * 64, "i": doc_id},
        )


def _cert() -> tuple[Any, Any]:
    from asn1crypto import keys as asn1_keys
    from asn1crypto import x509 as asn1_x509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Asha Rao (Test DSC)"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Talent Staffing Pvt Ltd"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(424242)
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False), critical=True)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    ak = asn1_keys.PrivateKeyInfo.load(
        key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)), ak


async def sign_pdf(pdf: bytes) -> bytes:
    """Test-only PAdES signer (incremental update over ``pdf``)."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    cert, key = _cert()
    signer = signers.SimpleSigner(signing_cert=cert, signing_key=key, cert_registry=None)
    out = io.BytesIO()
    pdf_signer = signers.PdfSigner(
        signers.PdfSignatureMetadata(field_name="DSC", subfilter=fields.SigSeedSubFilter.PADES), signer=signer
    )
    await pdf_signer.async_sign_pdf(IncrementalPdfFileWriter(io.BytesIO(pdf), strict=False), output=out)
    return out.getvalue()


async def _upload(org: BOrg, kind_path: str, doc_id: uuid.UUID, data: bytes) -> Any:
    return await org.c.post(
        f"{API}/billing/{kind_path}/{doc_id}/dsc/upload",
        files={"file": ("signed.pdf", data, "application/pdf")},
        headers=org.h(),
    )


# ---------------------------------------------------------------- tests
async def test_no_policy_keeps_delivery_unchanged(borg: BOrg) -> None:
    inv_id = await _issued_invoice(borg)
    inv = await _doc(borg, inv_id)
    assert inv.dsc_status == "not_required" and inv.signed_pdf_file_id is None
    async with tenant_session(borg.tenant_id) as db:
        assert (await db.execute(select(DscSignRequest))).first() is None
    status = await ok(await borg.c.get(f"{API}/billing/invoices/{inv_id}/dsc"))
    assert status["required"] is False and status["dsc_status"] == "not_required"


async def test_fake_adapter_signs_after_commit_and_delivers_signed_file(
    borg: BOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _policy(borg)
    await _fake_connector(borg)
    inv_id = await _issued_invoice(borg)
    inv = await _doc(borg, inv_id)
    assert inv.dsc_status == "signed" and inv.pdf_file_id and inv.signed_pdf_file_id and inv.dsc_signed_at
    assert [c[0] for c in FakeDscSigner.calls] == ["sign_pdf"]
    async with tenant_session(borg.tenant_id) as db:
        document = (
            await db.execute(
                select(Document).where(Document.owner_kind == "invoice", Document.owner_id == inv_id)
            )
        ).scalar_one()
        assert (
            document.access_level == "finance" and document.retention_class == "financial" and document.signed
        )
        versions = list(
            (
                await db.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document.id)
                    .order_by(DocumentVersion.version_no)
                )
            ).scalars()
        )
        assert [(v.version_no, v.source) for v in versions] == [(1, "generated"), (2, "signed")]
        assert versions[0].file_id == inv.pdf_file_id and versions[1].file_id == inv.signed_pdf_file_id
        # content hash unchanged and still reproducible (DSC columns are operational)
        fresh = await db.get(Invoice, inv_id)
        assert await issue.content_hash(db, "invoice", fresh) == inv.content_hash
        body, filename = await dsc.delivered_pdf(borg.ctx(), db, "invoice", fresh)
        assert body.endswith(MARKER) and filename.endswith("-signed.pdf")
    # e-mail delivery attaches the signed file
    await _set_irn_registered(borg, "invoices", inv_id)
    sent: list[Any] = []

    async def capture(message: Any) -> None:
        sent.append(message)

    from app.core import mailer

    monkeypatch.setattr(mailer, "send_email", capture)
    await ok(
        await borg.c.post(
            f"{API}/billing/invoices/{inv_id}/send", json={"to": ["ap@client.test"]}, headers=borg.h()
        )
    )
    assert sent and sent[0].attachments[0].data.endswith(MARKER)
    # a duplicate event is idempotent
    async with tenant_session(borg.tenant_id) as db:
        assert await dsc.sign_document(borg.ctx(), db, "invoice", inv_id) == "signed"
    assert len(FakeDscSigner.calls) == 1


async def test_manual_adapter_pending_gate_queue_and_uploads(borg: BOrg) -> None:
    await _policy(borg)
    inv_id = await _issued_invoice(borg)
    inv = await _doc(borg, inv_id)
    assert inv.dsc_status == "pending" and inv.pdf_file_id is not None and inv.signed_pdf_file_id is None
    content_hash = inv.content_hash
    # duplicate event -> still one request, no second notification round
    async with tenant_session(borg.tenant_id) as db:
        assert await dsc.sign_document(borg.ctx(), db, "invoice", inv_id) == "pending"
        assert len(list((await db.execute(select(DscSignRequest))).scalars())) == 1
    queue = await ok(await borg.c.get(f"{API}/billing/dsc/pending"))
    assert (
        [q["doc_id"] for q in queue] == [str(inv_id)]
        and queue[0]["adapter_key"] == "manual"
        and queue[0]["age_hours"] >= 0
    )
    # delivery gate: e-mail refused, Client Portal PDF hidden
    await _set_irn_registered(borg, "invoices", inv_id)
    err(
        await borg.c.post(
            f"{API}/billing/invoices/{inv_id}/send", json={"to": ["ap@client.test"]}, headers=borg.h()
        ),
        409,
        "dsc_pending",
    )
    async with tenant_session(borg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await portal.client_pdf(borg.ctx(), db, "invoice", inv_id)
        assert exc.value.code == "dsc_pending"
        detail = await portal.client_detail(borg.ctx(), db, "invoice", inv_id)
        assert detail.pdf_available is False and detail.number == inv.number
    # the stored render to sign
    resp = await borg.c.get(f"{API}/billing/invoices/{inv_id}/dsc/render")
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF")
    render = resp.content
    # refusals
    tampered = bytearray(render)
    tampered[len(render) // 2] = (tampered[len(render) // 2] + 1) % 256
    err(await _upload(borg, "invoices", inv_id, await sign_pdf(bytes(tampered))), 422, "dsc_content_mismatch")
    err(await _upload(borg, "invoices", inv_id, render + b"\n%%EOF\n"), 422, "dsc_signature_missing")
    good = await sign_pdf(render)
    broken = bytearray(good)
    contents_at = good.index(b"/Contents <", len(render)) + len(b"/Contents <") + 40
    broken[contents_at] = ord("0") if broken[contents_at] != ord("0") else ord("1")
    err(await _upload(borg, "invoices", inv_id, bytes(broken)), 422, "dsc_signature_invalid")
    err(await _upload(borg, "invoices", inv_id, b"not a pdf at all"), 422, "unsupported_file_type")
    # accepted
    out = await ok(await _upload(borg, "invoices", inv_id, good))
    assert (
        out["status"]["dsc_status"] == "signed"
        and out["verification"]["intact"]
        and out["verification"]["valid"]
    )
    assert out["verification"]["trusted"] == "unknown" and out["status"]["signer_serial"] == "424242"
    assert "Asha Rao" in (out["status"]["signer_subject"] or "")
    inv = await _doc(borg, inv_id)
    assert inv.dsc_status == "signed" and inv.content_hash == content_hash
    err(await _upload(borg, "invoices", inv_id, good), 409, "dsc_not_pending")
    async with tenant_session(borg.tenant_id) as db:
        body, _name = await portal.client_pdf(borg.ctx(), db, "invoice", inv_id)
        assert body == good
    assert await ok(await borg.c.get(f"{API}/billing/dsc/pending")) == []


async def test_immutability_and_period_guard_allow_list(borg: BOrg) -> None:
    await _policy(borg)
    inv_id = await _issued_invoice(borg)
    inv = await _doc(borg, inv_id)
    tid = borg.tenant_id
    with pytest.raises(DBAPIError):
        async with tenant_session(tid) as db:
            await db.execute(
                text(
                    "UPDATE invoices SET document_total = document_total + 1, dsc_status = 'signed' WHERE id = :i"
                ),
                {"i": inv_id},
            )
    # close the invoice's month: DSC columns may still change (signing finishes after close), amounts never
    async with tenant_session(tid) as db:
        await db.execute(
            text(
                "UPDATE accounting_periods SET status = 'closed' WHERE legal_entity_id = :le AND year = :y AND month = :m"
            ),
            {"le": borg.entity_id, "y": inv.doc_date.year, "m": inv.doc_date.month},
        )
    with pytest.raises(DBAPIError):
        async with tenant_session(tid) as db:
            await db.execute(text("UPDATE invoices SET notes = 'x' WHERE id = :i"), {"i": inv_id})
    render = (await borg.c.get(f"{API}/billing/invoices/{inv_id}/dsc/render")).content
    out = await ok(await _upload(borg, "invoices", inv_id, await sign_pdf(render)))
    assert out["status"]["dsc_status"] == "signed"
    after = await _doc(borg, inv_id)
    assert after.content_hash == inv.content_hash and after.document_total == inv.document_total


async def test_credit_note_same_flow_and_retry_after_failures(borg: BOrg) -> None:
    await _policy(borg)
    await _fake_connector(borg)
    inv_id = await _issued_invoice(borg, "5000.00")
    assert (await _doc(borg, inv_id)).dsc_status == "signed"
    FakeDscSigner.calls.clear()
    FakeDscSigner.fail_ops.add("sign_pdf")
    async with tenant_session(borg.tenant_id) as db:
        cn = await notes.create_credit_note(
            borg.ctx(), db, inv_id, CreditNoteIn(reason="commercial", gst_adjusted=False, amount=D("100.00"))
        )
        cn_id = cn.id
    async with tenant_session(borg.tenant_id) as db:
        await notes.issue_note(borg.ctx(), db, "credit_note", cn_id)
    note = await _doc(borg, cn_id, CreditNote)
    assert note.dsc_status == "failed"
    assert len([c for c in FakeDscSigner.calls if c[0] == "sign_pdf"]) == 3
    queue = await ok(await borg.c.get(f"{API}/billing/dsc/pending"))
    assert any(
        q["doc_id"] == str(cn_id) and q["status"] == "failed" and q["kind"] == "credit_note" for q in queue
    )
    FakeDscSigner.fail_ops.clear()
    out = await ok(await borg.c.post(f"{API}/billing/credit-notes/{cn_id}/dsc/retry", headers=borg.h()))
    assert out["dsc_status"] in ("pending", "signed")
    note = await _doc(borg, cn_id, CreditNote)
    assert note.dsc_status == "signed" and note.signed_pdf_file_id is not None
    err(
        await borg.c.post(f"{API}/billing/credit-notes/{cn_id}/dsc/retry", headers=borg.h()),
        409,
        "dsc_not_failed",
    )


async def test_policy_api_resolution_and_isolation(borg: BOrg, other_borg: BOrg) -> None:
    default = dict(
        await ok(
            await borg.c.post(
                f"{API}/dsc/policies",
                json={"gst_registration_id": str(borg.registration_id)},
                headers=borg.h(),
            ),
            201,
        )
    )
    assert default["client_id"] is None and default["required"] is True
    err(
        await borg.c.post(
            f"{API}/dsc/policies", json={"gst_registration_id": str(borg.registration_id)}, headers=borg.h()
        ),
        409,
        "dsc_policy_exists",
    )
    specific = await _policy(borg, required=False)
    async with tenant_session(borg.tenant_id) as db:
        p = await dsc.resolve_policy(db, borg.registration_id, borg.client_id)
        assert p is not None and str(p.id) == specific["id"]
        p2 = await dsc.resolve_policy(db, borg.registration_id, uuid.uuid4())
        assert p2 is not None and str(p2.id) == default["id"]
    # the exact (not required) policy wins -> not_required
    inv_id = await _issued_invoice(borg)
    assert (await _doc(borg, inv_id)).dsc_status == "not_required"
    patched = await ok(
        await borg.c.patch(
            f"{API}/dsc/policies/{specific['id']}",
            json={"required": True, "location": "Bengaluru"},
            headers=borg.h(),
        )
    )
    assert patched["required"] is True and patched["location"] == "Bengaluru"
    # cross-tenant: 404
    assert (
        await other_borg.c.patch(
            f"{API}/dsc/policies/{specific['id']}", json={"required": False}, headers=other_borg.h()
        )
    ).status_code == 404
    assert (await other_borg.c.get(f"{API}/billing/invoices/{inv_id}/dsc")).status_code == 404
    assert await ok(await other_borg.c.get(f"{API}/dsc/policies")) == []
    resp = await borg.c.delete(f"{API}/dsc/policies/{specific['id']}", headers=borg.h())
    assert resp.status_code == 204


async def test_digest_sweep_requeues_lost_signing_task(borg: BOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    await _policy(borg)
    await _fake_connector(borg)
    monkeypatch.setattr(dsc, "take_marked", lambda *_a: False)  # simulate the after-commit task being lost
    inv_id = await _issued_invoice(borg)
    assert (await _doc(borg, inv_id)).dsc_status == "pending"
    async with tenant_session(borg.tenant_id) as db:
        assert (await db.execute(select(DscSignRequest))).first() is None
        await dsc.pending_digest(borg.ctx(), db)
    assert (await _doc(borg, inv_id)).dsc_status == "signed"
