# ruff: noqa: E501
"""Epic 71 (stories 71.4-71.6): Tally XML builder, Zoho Books contract (recorded fixtures), document push with the
Fake ledger (mappings, drafts, closed periods, idempotency, cancel -> void), payment suggestions (★ confirm, echo rule)."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.integrations import MappingSecretBag, make_context
from app.models.billing import Invoice, InvoiceLine, InvoiceTaxLine
from app.models.ledger_sync import LedgerSyncRecord, PaymentSuggestion
from app.models.receivables import Payment
from app.models.tasks import Task
from app.schemas.ledger_sync import LedgerMappingIn
from app.services import ledger_sync_service as svc
from app.services.connectors.accounting.fake import FakeAccountingExporter
from app.services.connectors.accounting.manifests import ZOHO_BOOKS
from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO
from app.services.connectors.accounting.tally_xml import TallyConfig, build_import_xml, voucher_row
from app.services.ledger_sync.common import category_installation
from app.services.ledger_sync.push import push_records
from tests.hrms_sync.helpers import drain, human, install, system
from tests.modules.receivables.conftest import ROrg, close_month

MAPS = [
    LedgerMappingIn(kind="revenue", key="*", external_ref="Sales - Services"),
    LedgerMappingIn(kind="gst_output", key="igst:18", external_ref="Output IGST 18%"),
]


def _dto(kind: str = "invoice") -> LedgerDocumentDTO:
    doc_id = uuid.UUID(int=1)
    payload = {
        "lines": [{"revenue_ref": "Sales - Services", "inr_taxable_value": "1000.00", "taxes": [{"ledger_ref": "Output IGST 18%", "inr_amount": "180.00"}]}],
        "inr_round_off": "0", "irn": "a" * 64, "place_of_supply_name": "Karnataka", "currency": "INR", "linked_invoice_number": "INV-1",
    }
    return LedgerDocumentDTO(doc_id, 1, kind, "INV-1" if kind == "invoice" else "CN-1", date(2026, 10, 5), uuid.UUID(int=2), "Acme & Co", payload)  # type: ignore[arg-type]


def test_tally_xml_vouchers_and_masters() -> None:
    cfg = TallyConfig(company_name="Initech <IN>")
    xml = build_import_xml(cfg, [voucher_row(_dto())], [{"name": "Acme & Co", "gstin": "29ABCDE1234F1Z5", "state": "Karnataka"}])
    assert xml == build_import_xml(cfg, [voucher_row(_dto())], [{"name": "Acme & Co", "gstin": "29ABCDE1234F1Z5", "state": "Karnataka"}])
    root = ET.fromstring(xml)
    assert [r.text for r in root.iter("REPORTNAME")] == ["All Masters", "Vouchers"]
    assert root.find(".//SVCURRENTCOMPANY").text == "Initech <IN>"  # type: ignore[union-attr]
    v = root.find(".//VOUCHER")
    assert v is not None and v.get("REMOTEID") == f"talentos-{uuid.UUID(int=1)}-v1" and v.get("VCHTYPE") == "Sales"
    entries = {e.findtext("LEDGERNAME"): (e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT")) for e in v.iter("ALLLEDGERENTRIES.LIST")}
    assert entries == {"Acme & Co": ("Yes", "-1180.00"), "Sales - Services": ("No", "1000.00"), "Output IGST 18%": ("No", "180.00")}
    assert v.find(".//BILLTYPE").text == "New Ref"  # type: ignore[union-attr]
    cn = ET.fromstring(build_import_xml(cfg, [voucher_row(_dto("credit_note"))])).find(".//VOUCHER")
    assert cn is not None and cn.findtext(".//BILLTYPE") == "Agst Ref" and cn.findtext(".//BILLALLOCATIONS.LIST/NAME") == "INV-1"


async def test_zoho_contract_recorded_fixtures() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert req.url.params["organization_id"] == "600"
        if req.url.path == "/books/v3/invoices" and req.method == "POST":
            assert req.url.params["ignore_auto_number_generation"] == "true"
            return httpx.Response(201, json={"code": 0, "invoice": {"invoice_id": "Z1"}})
        if req.url.path.endswith("/status/sent"):
            return httpx.Response(200, json={"code": 0})
        if req.url.path == "/books/v3/customerpayments":
            return httpx.Response(200, json={"code": 0, "customerpayments": [{"payment_id": "P1"}], "page_context": {"has_more_page": False}})
        if req.url.path == "/books/v3/customerpayments/P1":
            return httpx.Response(200, json={"code": 0, "payment": {"date": "2026-10-20", "amount": 1000, "currency_code": "INR", "payment_mode": "banktransfer", "invoices": [{"invoice_id": "Z1", "invoice_number": "INV-1", "amount_applied": 1000, "tax_amount_withheld": 20}]}})
        return httpx.Response(400, json={"code": 1001, "message": "bad"})

    ctx = make_context(ZOHO_BOOKS, tenant_id=uuid.uuid4(), installation_id=uuid.uuid4(), config_id=None, settings={"organization_id": "600"},
                       secrets=MappingSecretBag({"oauth_refresh": "r", "client_id": "c", "client_secret": "s"}), mode="live", transport=httpx.MockTransport(handler))
    adapter = ZOHO_BOOKS.adapters["live"](ctx)
    res = await adapter.push_documents([_dto()])
    assert res[0].status == "synced" and res[0].external_ref == "Z1"
    pays, nxt = await adapter.pull_payments(None, None)
    assert nxt is None and pays[0].invoice_external_ref == "Z1" and pays[0].tds_amount == Decimal(20) and pays[0].mode == "neft"
    bad = await adapter.push_documents([LedgerDocumentDTO(uuid.uuid4(), 1, "credit_note", "CN-9", date(2026, 10, 5), uuid.uuid4(), "c", {"lines": []})])
    assert bad[0].status == "failed" and "bad" in (bad[0].message or "")
    assert seen  # every call went through the Egress-Guarded client (manifest hosts only)


async def _issued_invoice(org: ROrg, total: str = "1180", doc_date: date = date(2026, 10, 5)) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(Invoice(id=inv_id, tenant_id=org.tenant_id, legal_entity_id=org.entity_id, client_id=org.client_id, number=f"INV{uuid.uuid4().hex[:8].upper()}",
                       doc_date=doc_date, due_date=doc_date, status="draft", currency="INR", document_total=Decimal(total), taxable_total=Decimal(1000),
                       inr_document_total=Decimal(total), charged_tax_total=Decimal(180), fiscal_year="26-27", pos_state_code="29"))
        await db.flush()
        line = InvoiceLine(id=uuid.uuid4(), tenant_id=org.tenant_id, invoice_id=inv_id, line_no=1, line_kind="time", description="Services", sac_code="998313",
                           quantity=Decimal(1), rate=Decimal(1000), effective_rate=Decimal(1000), gross_amount=Decimal(1000), taxable_value=Decimal(1000), inr_taxable_value=Decimal(1000))
        db.add(line)
        await db.flush()
        db.add(InvoiceTaxLine(id=uuid.uuid4(), tenant_id=org.tenant_id, invoice_id=inv_id, line_id=line.id, component="igst", rate_pct=Decimal(18), amount=Decimal(180), inr_amount=Decimal(180)))
        await db.flush()
        await db.execute(update(Invoice).where(Invoice.id == inv_id).values(status="issued"))
    return inv_id


async def _rec(org: ROrg, doc_id: uuid.UUID) -> LedgerSyncRecord | None:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(select(LedgerSyncRecord).where(LedgerSyncRecord.document_id == doc_id))).scalar_one_or_none()


async def test_push_mappings_periods_idempotency_and_cancel(org: ROrg) -> None:
    inst, _ = await install(org, "zoho_books", config={"organization_id": "600"})
    good = await _issued_invoice(org)
    draft = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(Invoice(id=draft, tenant_id=org.tenant_id, legal_entity_id=org.entity_id, client_id=org.client_id, status="draft", currency="INR", doc_date=date(2026, 10, 6)))
    # missing mapping -> failed + one Task per key
    async with tenant_session(org.tenant_id) as db:
        ei = await category_installation(db, "accounting", org.entity_id)
        assert ei is not None
        await push_records(system(org), db, ei, org.entity_id, None, None)
    rec = await _rec(org, good)
    assert rec is not None and rec.status == "failed" and "Missing mapping: revenue *" in (rec.message or "")
    assert await _rec(org, draft) is None
    async with tenant_session(org.tenant_id) as db:
        assert (await db.execute(select(Task).where(Task.entity_type == "ledger_sync"))).scalars().all()
        await svc.put_mappings(human(org), db, inst, org.entity_id, [*MAPS, LedgerMappingIn(kind="customer", key=str(org.client_id), external_ref="CUST-1")])
    async with tenant_session(org.tenant_id) as db:
        ei = await category_installation(db, "accounting", org.entity_id)
        counts = await push_records(system(org), db, ei, org.entity_id, None, None)  # type: ignore[arg-type]
    rec = await _rec(org, good)
    assert rec is not None and rec.status == "synced" and rec.external_ref == "FAKE-LEDGER-1", counts
    async with tenant_session(org.tenant_id) as db:
        ei = await category_installation(db, "accounting", org.entity_id)
        await push_records(system(org), db, ei, org.entity_id, None, None)  # type: ignore[arg-type]
    assert [c for c in FakeAccountingExporter.calls if c[0] == "push_documents"].__len__() == 1
    # cancel -> out_of_sync -> next push voids (version 2)
    async with tenant_session(org.tenant_id) as db:
        await db.execute(update(Invoice).where(Invoice.id == good).values(status="cancelled", cancel_basis="full_credit_note"))
        ei = await category_installation(db, "accounting", org.entity_id)
        await push_records(system(org), db, ei, org.entity_id, None, None)  # type: ignore[arg-type]
    rec = await _rec(org, good)
    assert rec is not None and rec.document_version == 2 and rec.synced_version == 2 and rec.status == "synced"
    # closed period -> blocked_period
    old = await _issued_invoice(org, doc_date=date(2026, 9, 10))
    await close_month(org, 2026, 9)
    async with tenant_session(org.tenant_id) as db:
        ei = await category_installation(db, "accounting", org.entity_id)
        await push_records(system(org), db, ei, org.entity_id, None, None)  # type: ignore[arg-type]
    rec = await _rec(org, old)
    assert rec is not None and rec.status == "blocked_period"


async def test_suggestions_confirm_star_and_echo(org: ROrg) -> None:
    inst, _ = await install(org, "zoho_books", config={"organization_id": "600"})
    inv_id = await _issued_invoice(org)
    async with tenant_session(org.tenant_id) as db:
        number = (await db.get(Invoice, inv_id)).number  # type: ignore[union-attr]
    FakeAccountingExporter.payments.append(LedgerPaymentDTO("ZP-1", number, None, Decimal(1000), "INR", date(2026, 10, 20), Decimal(180), "neft", "UTR1"))
    FakeAccountingExporter.payments.append(LedgerPaymentDTO("ZP-2", "NOPE-1", None, Decimal(5), "INR", date(2026, 10, 20), None, None, None))
    from app.services import integrations_service

    for _ in range(2):  # dedup by external ref
        async with tenant_session(org.tenant_id) as db:
            await integrations_service.run_sync(human(org), db, inst, "pull_payments")
        await drain(org)
    async with tenant_session(org.tenant_id) as db:
        rows = {s.external_ref: s for s in (await db.execute(select(PaymentSuggestion))).scalars()}
    assert set(rows) == {"ZP-1", "ZP-2"} and rows["ZP-1"].invoice_id == inv_id and rows["ZP-2"].match_note == "invoice_not_found"
    sid = rows["ZP-1"].id
    for kind in ("app", "agent"):
        async with tenant_session(org.tenant_id) as db:
            with pytest.raises(Forbidden) as exc:
                await svc.confirm(human(org, principal_type=kind), db, sid)
        assert exc.value.code == "human_only_action"
    async with tenant_session(org.tenant_id) as db:
        out = await svc.confirm(human(org), db, sid)
    assert out.status == "confirmed" and out.payment_id is not None
    async with tenant_session(org.tenant_id) as db:
        pay = await db.get(Payment, out.payment_id)
        assert pay is not None and pay.created_by == org.admin.user_id and pay.amount == Decimal(1000)
        await svc.put_settings(human(org), db, __import__("app.schemas.ledger_sync", fromlist=["LedgerSettingsIn"]).LedgerSettingsIn(auto_push_on_issue=True))
        await svc.put_mappings(human(org), db, inst, org.entity_id, [*MAPS, LedgerMappingIn(kind="customer", key=str(org.client_id), external_ref="CUST-1")])
        ei = await category_installation(db, "accounting", org.entity_id)
        await push_records(system(org), db, ei, org.entity_id, None, None)  # type: ignore[arg-type]
    rec = await _rec(org, out.payment_id)
    assert rec is not None and rec.status == "synced" and rec.external_ref == "ZP-1"
    pushed: list[Any] = [c[1] for c in FakeAccountingExporter.calls if c[0] == "push_documents"]
    assert all(out.payment_id not in ids for ids in pushed)  # echo rule: never pushed back
    async with tenant_session(org.tenant_id) as db:
        rej = await svc.reject(human(org), db, rows["ZP-2"].id, "Not ours")
    assert rej.status == "rejected" and rej.reject_reason == "Not ours"


async def test_api_cross_tenant(org: ROrg, other: ROrg) -> None:
    inst, _ = await install(org, "tally_xml", config={"company_name": "Initech"})
    await _issued_invoice(org)
    r = await org.c.put(f"/api/v1/ledger/installations/{inst}/entities/{org.entity_id}/mappings", json={"items": [m.model_dump() for m in MAPS]}, headers=org.h())
    assert r.status_code == 200, r.text
    r = await org.c.post("/api/v1/ledger/runs", json={"kind": "tally_file", "legal_entity_id": str(org.entity_id), "from_date": "2026-10-01", "to_date": "2026-10-31", "include_masters": True}, headers=org.h("k-71"))
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "completed"
    f = await org.c.get(f"/api/v1/ledger/runs/{run['id']}/file")
    assert f.status_code == 200 and b"<VOUCHER REMOTEID=" in f.content and b"All Masters" in f.content
    assert (await other.c.get(f"/api/v1/ledger/runs/{run['id']}/file")).status_code == 404
    assert (await other.c.get("/api/v1/ledger/records")).json()["total"] == 0
