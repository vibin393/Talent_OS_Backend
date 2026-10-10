"""Epic 29 end-to-end: runs -> drafts -> issue -> PDF -> e-invoice export / import -> notes -> integrity."""

import json
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.billing import Invoice
from app.models.postings import BillingPosting
from app.schemas.legal_entities import FxRateIn
from app.services import legal_entities_service
from app.services.billing import einvoice_schema
from tests.modules.billing.conftest import (
    B,
    BOrg,
    add_lut,
    billing_profile,
    err,
    hours,
    issue,
    ok,
    placement,
    run,
)
from tests.modules.demands.helpers import create_client

D = Decimal


async def _states(org: BOrg, source_id: uuid.UUID) -> list[str]:
    async with tenant_session(org.tenant_id) as db:
        rows = await db.execute(select(BillingPosting.state).where(BillingPosting.source_id == source_id))
        return sorted(rows.scalars())


async def test_acceptance_three_placements_issue_einvoice_notes(borg: BOrg) -> None:
    org = borg
    mh = await create_client(org.admin, "Mumbai Client")
    await billing_profile(org, mh, state="27")
    us = await create_client(org.admin, "US Client")
    await billing_profile(org, us, state=None, treatment="export", country="US", with_gstin=False)
    await add_lut(org)
    async with tenant_session(org.tenant_id) as db:
        await legal_entities_service.upsert_rate(
            org.ctx(),
            db,
            FxRateIn(
                from_currency="USD",
                to_currency="INR",
                rate_date=date.today(),
                rate=D("83.250000"),
                source="RBI",
            ),
        )
    p_ka = await placement(org)
    p_mh = await placement(org, mh)
    p_us = await placement(org, us, currency="USD", bill_rate="100.00")
    p_none = await placement(org)
    async with tenant_session(org.tenant_id) as db:
        from sqlalchemy import text

        await db.execute(text("DELETE FROM billing_rules WHERE placement_id = :p"), {"p": p_none})
    s_ka = hours(org, p_ka, "8", date(2026, 9, 1))
    hours(org, p_ka, "8", date(2026, 9, 2))
    hours(org, p_ka, "2", date(2026, 9, 2), category="ot", multiplier="1.5")
    hours(org, p_mh, "10", date(2026, 9, 3))
    hours(org, p_us, "10", date(2026, 9, 3), currency="USD")
    hours(org, p_none, "5", date(2026, 9, 3))

    r = await run(org)
    assert r["status"] == "built", r
    assert r["draft_count"] == 3, r
    assert {e["kind"] for e in r["exceptions"]} >= {"billing_rule_missing"}
    drafts = {d["client_id"]: d for d in r["drafts"]}
    ka = await ok(await org.c.get(f"{B}/invoices/{drafts[str(org.client_id)]['id']}"))
    assert ka["status"] == "draft" and ka["number"] is None
    assert ka["supply_type"] == "b2b" and ka["intra_state"] is True
    # 16 h x 1500 + 2 h x 2250 (OT 1.5) = 28,500; CGST + SGST 9 % each
    assert D(ka["taxable_total"]) == D("28500.00")
    assert D(ka["charged_tax_total"]) == D("5130.00")
    assert {t["component"] for ln in ka["lines"] for t in ln["taxes"]} == {"cgst", "sgst"}

    out = await ok(
        await org.c.post(
            f"{B}/invoices/issue-batch",
            json={"invoice_ids": [d["id"] for d in r["drafts"]]},
            headers=org.h(str(uuid.uuid4())),
        )
    )
    assert out["issued"] == 3, out
    numbers = sorted(x["number"] for x in out["results"])
    assert numbers == ["INV/26-27/0001", "INV/26-27/0002", "INV/26-27/0003"]

    mh_inv = await ok(await org.c.get(f"{B}/invoices/{drafts[str(mh)]['id']}"))
    assert [t["component"] for t in mh_inv["lines"][0]["taxes"]] == ["igst"]
    us_inv = await ok(await org.c.get(f"{B}/invoices/{drafts[str(us)]['id']}"))
    assert us_inv["supply_type"] == "expwop" and D(us_inv["charged_tax_total"]) == 0
    assert D(us_inv["inr_taxable_total"]) == D("83250.00") and us_inv["lut_ref"]
    assert us_inv["einvoice_status"] == "pending_irn" and ka["einvoice_status"] == "not_applicable"

    pdf = await org.c.get(f"{B}/invoices/{ka['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # send is refused while the IRN is pending
    err(
        await org.c.post(
            f"{B}/invoices/{us_inv['id']}/send", json={"to": ["ap@client.example.com"]}, headers=org.h()
        ),
        409,
        "pending_irn",
    )

    exported = await org.c.get(
        f"{B}/einvoice/export", params={"gst_registration_id": str(org.registration_id)}
    )
    payloads = json.loads(exported.text)
    assert len(payloads) == 3
    for p in payloads:
        assert einvoice_schema.validate(p) == [], p
    hash_before = {
        d: (await ok(await org.c.get(f"{B}/invoices/{d}")))["content_hash"] for d in (us_inv["id"],)
    }
    response = [
        {
            "SellerGstin": p["SellerDtls"]["Gstin"],
            "DocDtls": p["DocDtls"],
            "Irn": uuid.uuid4().hex + uuid.uuid4().hex,
            "AckNo": f"1123{i}",
            "AckDt": "2026-10-05 10:00:00",
            "SignedQRCode": "eyJhbGciOi.signed.qr",
        }
        for i, p in enumerate(payloads)
    ]
    files = {"file": ("response.json", json.dumps(response).encode(), "application/json")}
    preview = await ok(await org.c.post(f"{B}/einvoice/import", files=files, headers=org.h()), 201)
    assert preview["counts"].get("matched") == 3, preview
    applied = await ok(
        await org.c.post(f"{B}/einvoice/import/{preview['batch_id']}/apply", headers=org.h(str(uuid.uuid4())))
    )
    assert applied["applied"] == 3
    again = await ok(await org.c.post(f"{B}/einvoice/import", files=files, headers=org.h()), 201)
    assert again["duplicate_of"] == preview["batch_id"] and again["counts"].get("already_registered") == 3
    us_after = await ok(await org.c.get(f"{B}/invoices/{us_inv['id']}"))
    assert (
        us_after["einvoice_status"] == "irn_registered"
        and us_after["content_hash"] == hash_before[us_inv["id"]]
    )
    assert (await org.c.get(f"{B}/invoices/{us_inv['id']}/pdf")).content.startswith(b"%PDF")

    # a full credit note cancels the KA invoice and frees its hours for re-billing
    assert await _states(org, s_ka) == ["posted"]
    cn = await ok(
        await org.c.post(
            f"{B}/invoices/{ka['id']}/credit-notes",
            json={"reason": "cancellation", "full": True, "rebill": True},
            headers=org.h(),
        ),
        201,
    )
    cn_issued = await ok(
        await org.c.post(f"{B}/credit-notes/{cn['id']}/issue", json={}, headers=org.h(str(uuid.uuid4())))
    )
    assert cn_issued["number"] == "CN/26-27/0001"
    assert D(cn_issued["document_total"]) == D(ka["document_total"])
    ka_after = await ok(await org.c.get(f"{B}/invoices/{ka['id']}"))
    assert ka_after["status"] == "cancelled" and ka_after["cancel_basis"] == "full_credit_note"
    assert await _states(org, s_ka) == ["released"]
    r2 = await run(org)
    assert r2["draft_count"] == 1 and await _states(org, s_ka) == ["released", "reserved"]

    findings = await ok(await org.c.post(f"{B}/integrity/run", headers=org.h()))
    assert findings == [], findings


async def test_rate_correction_becomes_debit_note(borg: BOrg) -> None:
    org = borg
    p = await placement(org)
    sid = hours(org, p, "10", date(2026, 9, 1))
    r = await run(org)
    inv = r["drafts"][0]
    await issue(org, inv["id"])
    from tests.modules.billing.conftest import FAKE

    FAKE[str(org.tenant_id)].clear()
    hours(
        org,
        p,
        "10",
        date(2026, 9, 1),
        kind="rate_adjustment",
        rate_delta=D("100.00"),
        multiplier=D(1),
        original_source_kind="test_hours",
        original_source_id=sid,
    )
    await run(org)
    dns = await ok(await org.c.get(f"{B}/debit-notes", params={"invoice_id": inv["id"]}))
    assert len(dns) == 1
    dn = await ok(await org.c.get(f"{B}/debit-notes/{dns[0]['id']}"))
    assert dn["reason"] == "rate_correction" and D(dn["taxable_total"]) == D("1000.00")
    issued = await ok(
        await org.c.post(f"{B}/debit-notes/{dn['id']}/issue", json={}, headers=org.h(str(uuid.uuid4())))
    )
    assert issued["number"] == "DN/26-27/0001" and D(issued["charged_tax_total"]) == D("180.00")


async def test_exactly_once_discard_and_partial_cn(borg: BOrg) -> None:
    org = borg
    p = await placement(org)
    sid = hours(org, p, "8", date(2026, 9, 1))
    first = await run(org)
    assert first["draft_count"] == 1
    second = await run(org)
    assert second["draft_count"] == 0  # the item is reserved on the first run's draft
    await ok(await org.c.post(f"{B}/runs/{first['id']}/discard", headers=org.h()))
    assert await _states(org, sid) == ["released"]
    third = await run(org)
    inv = third["drafts"][0]
    issued = (await issue(org, inv["id"])).json()
    line = issued["lines"][0]
    cn = await ok(
        await org.c.post(
            f"{B}/invoices/{inv['id']}/credit-notes",
            json={
                "reason": "discount",
                "lines": [{"original_invoice_line_id": line["id"], "amount": "100.00"}],
            },
            headers=org.h(),
        ),
        201,
    )
    await ok(
        await org.c.post(f"{B}/credit-notes/{cn['id']}/issue", json={}, headers=org.h(str(uuid.uuid4())))
    )
    assert (await _states(org, sid)).count("posted") == 1  # a partial CN never releases
    # manual partial CNs cannot claim hours corrections
    err(
        await org.c.post(
            f"{B}/invoices/{inv['id']}/credit-notes",
            json={
                "reason": "hours_correction",
                "lines": [{"original_invoice_line_id": line["id"], "amount": "1.00"}],
            },
            headers=org.h(),
        ),
        422,
        "reason_not_allowed",
    )


async def test_idempotent_issue_stale_and_cross_tenant(borg: BOrg, other_borg: BOrg) -> None:
    org = borg
    p = await placement(org)
    hours(org, p, "8", date(2026, 9, 1))
    inv = (await run(org))["drafts"][0]
    key = str(uuid.uuid4())
    a = (await issue(org, inv["id"], key=key)).json()
    b = await issue(org, inv["id"], key=key)
    assert b.headers.get("Idempotency-Replayed") == "true" and b.json()["number"] == a["number"]
    reuse = await org.c.post(
        f"{B}/invoices/{inv['id']}/issue", json={"doc_date": "2026-10-06"}, headers=org.h(key)
    )
    assert reuse.status_code == 409 and reuse.json()["error"]["code"] == "idempotency_key_reused"
    again = (await issue(org, inv["id"])).json()
    assert again["number"] == a["number"]
    # another tenant never sees it
    assert (await other_borg.c.get(f"{B}/invoices/{inv['id']}")).status_code == 404
    assert (await other_borg.c.get(f"{B}/invoices/{inv['id']}/pdf")).status_code == 404
    err(
        await other_borg.c.post(
            f"{B}/invoices/{inv['id']}/issue", json={}, headers=other_borg.h(str(uuid.uuid4()))
        ),
        404,
        "not_found",
    )
    # G21: a tax rate effective on the issue date makes the draft stale
    hours(org, p, "4", date(2026, 9, 2))
    inv2 = (await run(org))["drafts"][0]
    await ok(
        await org.c.post(
            f"{B}/tax-rates",
            json={"sac_code": "998513", "rate_pct": "12", "effective_from": "2026-10-20"},
            headers=org.h(),
        ),
        201,
    )
    stale = await issue(org, inv2["id"], doc_date="2026-10-20", status=409)
    assert stale.json()["error"]["code"] == "draft_stale" and "diff" in stale.json()["error"]["details"]


async def test_manual_invoice_period_guard_and_back_dating(borg: BOrg) -> None:
    org = borg
    body = {
        "client_id": str(org.client_id),
        "legal_entity_id": str(org.entity_id),
        "lines": [{"description": "Setup fee", "rate": "1000.00"}],
    }
    inv = await ok(await org.c.post(f"{B}/invoices/manual", json=body, headers=org.h()), 201)
    assert inv["lines"][0]["is_manual"] is True and D(inv["document_total"]) == D("1180.00")
    # a month without an open accounting period
    closed = await org.c.post(
        f"{B}/invoices/{inv['id']}/issue", json={"doc_date": "2025-01-15"}, headers=org.h(str(uuid.uuid4()))
    )
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "period_closed"
    await issue(org, inv["id"], doc_date="2026-10-10")
    inv2 = await ok(await org.c.post(f"{B}/invoices/manual", json=body, headers=org.h()), 201)
    back = await org.c.post(
        f"{B}/invoices/{inv2['id']}/issue", json={"doc_date": "2026-10-05"}, headers=org.h(str(uuid.uuid4()))
    )
    assert back.status_code == 409 and back.json()["error"]["code"] in (
        "doc_date_out_of_order",
        "draft_stale",
    ), back.text
    async with tenant_session(org.tenant_id) as db:
        assert (
            await db.execute(select(Invoice.number).where(Invoice.id == uuid.UUID(inv2["id"])))
        ).scalar() is None


async def test_rebillable_expenses_and_opening_import(borg: BOrg) -> None:
    """Epic 27 ``expense_line`` items (bill amount, ``rebill_tax_mode``) become expense / pure-agent lines; an
    imported opening invoice raises the counter and refuses credit notes."""
    from app.schemas.billing import OpeningInvoiceRow
    from app.services import postings
    from app.services.billing import service as billing_service
    from tests.modules.billing.conftest import FAKE

    org = borg
    p = await placement(org)
    for mode, amount in (("taxable", "500.00"), ("pure_agent", "2000.00")):
        FAKE[str(org.tenant_id)].append(
            postings.SourceItem(
                "expense_line",
                uuid.uuid4(),
                p,
                date(2026, 9, 5),
                D(1),
                D(amount),
                "INR",
                {
                    "category": "travel",
                    "billable": True,
                    "unit": "item",
                    "rebill_tax_mode": mode,
                    "reimbursable": True,
                },
            )
        )
    inv = (await run(org))["drafts"][0]
    detail = await ok(await org.c.get(f"{B}/invoices/{inv['id']}"))
    kinds = {ln["line_kind"]: ln for ln in detail["lines"]}
    assert set(kinds) == {"expense", "pure_agent_expense"}
    assert kinds["pure_agent_expense"]["taxes"] == [] and D(detail["outside_taxable_total"]) == D("2000.00")
    assert D(detail["document_total"]) == D("2590.00")
    async with tenant_session(org.tenant_id) as db:
        row = OpeningInvoiceRow(
            number="INV/26-27/0040",
            doc_date=date(2026, 10, 1),
            client_id=org.client_id,
            legal_entity_id=org.entity_id,
            taxable_total=D("100.00"),
            tax_total=D("18.00"),
            document_total=D("118.00"),
            sequence=40,
        )
        out = await billing_service.import_opening_invoice(org.ctx(), db, row, uuid.uuid4())
    issued = (await issue(org, inv["id"])).json()
    assert issued["number"] == "INV/26-27/0041"
    err(
        await org.c.post(
            f"{B}/invoices/{out.id}/credit-notes",
            json={"reason": "cancellation", "full": True},
            headers=org.h(),
        ),
        409,
        "imported_invoice_no_credit_note",
    )
