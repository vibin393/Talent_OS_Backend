"""Epic 14 (FR-76): Legal Entity administration deltas -- readiness, field locks, PAN / state rules, GST
deactivation, LUT per FY, bank accounts (default per currency, archive, audited unmask), logo, business-unit
default entity, cross-tenant 404 and external principals refused."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.billing import Invoice
from app.models.identity import Role
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import csrf, join
from tests.modules.legal_entities.conftest import API, LeOrg, gstin
from tests.p2 import make_client_user, make_vendor, make_vendor_user, sign_in_external

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def h(org: LeOrg) -> dict[str, str]:
    return csrf(org.admin.client)


async def _entity(org: LeOrg, name: str = "Acme India", **extra: object) -> dict[str, object]:
    body = {"name": name, "legal_name": f"{name} Pvt Ltd", "pan": "ABCDE1234F", "state_code": "29", **extra}
    resp = await org.admin.client.post(f"{API}/legal-entities", json=body, headers=h(org))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_readiness_flow(leorg: LeOrg) -> None:
    c = leorg.admin.client
    eid = leorg.entity_id
    ready = (await c.get(f"{API}/legal-entities/{eid}/readiness")).json()
    assert ready["ready_for_invoicing"] is False
    assert set(ready["missing"]) == {
        "pan",
        "state_code",
        "principal_gst_registration",
        "bank_account",
        "signatory",
    }

    patched = await c.patch(
        f"{API}/legal-entities/{eid}",
        json={
            "pan": "ABCDE1234F",
            "state_code": "29",
            "signatory_name": "Asha Rao",
            "signatory_title": "Director",
        },
        headers=h(leorg),
    )
    assert patched.status_code == 200, patched.text
    # A Maharashtra-coded GSTIN is refused for a Karnataka registration.
    wrong_state = await c.post(
        f"{API}/legal-entities/{eid}/gst-registrations",
        json={"gstin": gstin("27"), "state_code": "29"},
        headers=h(leorg),
    )
    assert wrong_state.status_code == 422 and wrong_state.json()["error"]["code"] == "gstin_state_mismatch"
    reg = await c.post(
        f"{API}/legal-entities/{eid}/gst-registrations",
        json={"gstin": gstin("29"), "state_code": "29"},
        headers=h(leorg),
    )
    assert reg.status_code == 201, reg.text
    bank = await c.post(
        f"{API}/legal-entities/{eid}/bank-accounts",
        json={
            "label": "Main",
            "bank_name": "HDFC",
            "account_number": "50100012345678",
            "ifsc": "HDFC0001234",
            "branch": "MG Road",
            "account_name": "Acme",
        },
        headers=h(leorg),
    )
    assert bank.status_code == 201, bank.text
    assert bank.json()["branch"] == "MG Road" and bank.json()["account_number_last4"] == "5678"
    ready = (await c.get(f"{API}/legal-entities/{eid}/readiness")).json()
    assert ready == {"legal_entity_id": str(eid), "ready_for_invoicing": True, "missing": []}


async def test_pan_mismatch_and_one_active_registration_per_state(leorg: LeOrg) -> None:
    c = leorg.admin.client
    entity = await _entity(leorg)
    eid = entity["id"]
    first = await c.post(
        f"{API}/legal-entities/{eid}/gst-registrations",
        json={"gstin": gstin("29"), "state_code": "29"},
        headers=h(leorg),
    )
    assert first.status_code == 201
    second = await c.post(
        f"{API}/legal-entities/{eid}/gst-registrations",
        json={"gstin": gstin("29", entity="2"), "state_code": "29"},
        headers=h(leorg),
    )
    assert second.status_code == 409 and second.json()["error"]["code"] == "gst_registration_state_exists"
    pan = await c.patch(f"{API}/legal-entities/{eid}", json={"pan": "ZZZZZ9999Z"}, headers=h(leorg))
    assert pan.status_code == 422 and pan.json()["error"]["code"] == "pan_mismatch"

    off = await c.post(f"{API}/gst-registrations/{first.json()['id']}/deactivate", headers=h(leorg))
    assert off.status_code == 200 and off.json()["status"] == "inactive"
    # With the registration inactive the PAN may change and a new Karnataka registration is allowed.
    again = await c.post(
        f"{API}/legal-entities/{eid}/gst-registrations",
        json={"gstin": gstin("29", entity="2"), "state_code": "29"},
        headers=h(leorg),
    )
    assert again.status_code == 201, again.text
    back = await c.post(f"{API}/gst-registrations/{first.json()['id']}/reactivate", headers=h(leorg))
    assert back.status_code == 409


async def test_lut_put_per_fiscal_year(leorg: LeOrg) -> None:
    c = leorg.admin.client
    entity = await _entity(leorg)
    reg = (
        await c.post(
            f"{API}/legal-entities/{entity['id']}/gst-registrations",
            json={"gstin": gstin("29"), "state_code": "29"},
            headers=h(leorg),
        )
    ).json()
    body = {"lut_number": "AD290326000001", "valid_from": "2026-04-01", "valid_to": "2027-03-31"}
    put = await c.put(f"{API}/gst-registrations/{reg['id']}/luts/26-27", json=body, headers=h(leorg))
    assert put.status_code == 200 and put.json()["fiscal_year"] == "26-27"
    replaced = await c.put(
        f"{API}/gst-registrations/{reg['id']}/luts/26-27",
        json={**body, "lut_number": "AD2"},
        headers=h(leorg),
    )
    assert replaced.json()["lut_number"] == "AD2" and replaced.json()["id"] == put.json()["id"]
    assert [x["lut_number"] for x in (await c.get(f"{API}/gst-registrations/{reg['id']}/luts")).json()] == [
        "AD2"
    ]
    bad = await c.put(f"{API}/gst-registrations/{reg['id']}/luts/2026", json=body, headers=h(leorg))
    assert bad.status_code == 422


async def test_field_lock_after_invoice(leorg: LeOrg) -> None:
    c = leorg.admin.client
    entity = await _entity(leorg)
    client_id = await create_client(leorg.admin)
    async with tenant_session(leorg.tenant_id) as db:
        db.add(
            Invoice(
                id=uuid.uuid4(),
                tenant_id=leorg.tenant_id,
                legal_entity_id=uuid.UUID(str(entity["id"])),
                client_id=client_id,
                currency="INR",
            )
        )
    locked = await c.patch(
        f"{API}/legal-entities/{entity['id']}", json={"timezone": "Asia/Dubai"}, headers=h(leorg)
    )
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "legal_entity_field_locked"
    assert locked.json()["error"]["details"]["fields"] == ["timezone"]
    same = await c.patch(
        f"{API}/legal-entities/{entity['id']}",
        json={"timezone": "Asia/Kolkata", "signatory_name": "X"},
        headers=h(leorg),
    )
    assert same.status_code == 200
    gone = await c.delete(f"{API}/legal-entities/{entity['id']}", headers=h(leorg))
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "legal_entity_in_use"


async def test_bank_accounts_default_per_currency_archive_and_unmask(app: FastAPI, leorg: LeOrg) -> None:
    c = leorg.admin.client
    eid = leorg.entity_id
    base = {"bank_name": "HDFC", "ifsc": "HDFC0001234"}
    inr = (
        await c.post(
            f"{API}/legal-entities/{eid}/bank-accounts",
            json={**base, "label": "INR", "account_number": "11112222", "currency": "INR"},
            headers=h(leorg),
        )
    ).json()
    usd = (
        await c.post(
            f"{API}/legal-entities/{eid}/bank-accounts",
            json={**base, "label": "USD", "account_number": "33334444", "currency": "USD"},
            headers=h(leorg),
        )
    ).json()
    assert inr["is_default"] and usd["is_default"]  # one default per currency
    inr2 = (
        await c.post(
            f"{API}/legal-entities/{eid}/bank-accounts",
            json={
                **base,
                "label": "INR 2",
                "account_number": "55556666",
                "currency": "INR",
                "is_default": True,
            },
            headers=h(leorg),
        )
    ).json()
    listed = {a["label"]: a for a in (await c.get(f"{API}/legal-entities/{eid}/bank-accounts")).json()}
    assert listed["INR 2"]["is_default"] and not listed["INR"]["is_default"] and listed["USD"]["is_default"]

    archived = await c.patch(f"{API}/bank-accounts/{inr2['id']}", json={"archived": True}, headers=h(leorg))
    assert (
        archived.status_code == 200 and archived.json()["archived_at"] and not archived.json()["is_default"]
    )
    made = await c.patch(
        f"{API}/bank-accounts/{inr['id']}",
        json={"is_default": True, "branch": "Indiranagar"},
        headers=h(leorg),
    )
    assert made.json()["is_default"] and made.json()["branch"] == "Indiranagar"

    # Unmask: Tenant Admin holds identity:view; the read is audited.
    unmasked = await c.post(f"{API}/bank-accounts/{inr['id']}/unmask", headers=h(leorg))
    assert unmasked.status_code == 200 and unmasked.json()["account_number"] == "11112222"
    async with tenant_session(leorg.tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "bank_account.unmasked", AuditLog.entity_id == str(inr["id"])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # A Finance user (legal_entity:read, no identity:view) gets the FR-82 403.
    finance = await join(app, leorg.tenant, "finance")
    try:
        denied = await finance.client.post(f"{API}/bank-accounts/{inr['id']}/unmask", headers=finance.h())
        assert denied.status_code == 403
        details = denied.json()["error"]["details"]
        assert details["required_permission"] == "identity:view" and details["ask"] == "tenant_admin"
        assert details["action"]
        assert (await finance.client.get(f"{API}/legal-entities/{eid}/bank-accounts")).status_code == 200
    finally:
        await finance.client.aclose()


async def test_deactivate_with_reason_is_audited(leorg: LeOrg) -> None:
    c = leorg.admin.client
    entity = await _entity(leorg, "Short Lived")
    off = await c.post(
        f"{API}/legal-entities/{entity['id']}/deactivate",
        json={"reason": "Merged into parent"},
        headers=h(leorg),
    )
    assert off.status_code == 200
    assert off.json()["deactivation_reason"] == "Merged into parent" and off.json()["deactivated_at"]
    back = await c.post(f"{API}/legal-entities/{entity['id']}/reactivate", headers=h(leorg))
    assert back.json()["status"] == "active" and back.json()["deactivation_reason"] is None
    async with tenant_session(leorg.tenant_id) as db:
        actions = set(
            (
                await db.execute(select(AuditLog.action).where(AuditLog.entity_id == str(entity["id"])))
            ).scalars()
        )
    assert {"legal_entity.deactivated", "legal_entity.reactivated"} <= actions


async def test_logo_upload(leorg: LeOrg) -> None:
    c = leorg.admin.client
    ok = await c.post(
        f"{API}/legal-entities/{leorg.entity_id}/logo",
        files={"file": ("logo.png", PNG, "image/png")},
        headers=h(leorg),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["logo_file_id"]
    big = await c.post(
        f"{API}/legal-entities/{leorg.entity_id}/logo",
        files={"file": ("logo.png", PNG + b"\x00" * (1024 * 1024), "image/png")},
        headers=h(leorg),
    )
    assert big.status_code == 422 and big.json()["error"]["code"] == "file_too_large"
    pdf = await c.post(
        f"{API}/legal-entities/{leorg.entity_id}/logo",
        files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
        headers=h(leorg),
    )
    assert pdf.status_code == 422 and pdf.json()["error"]["code"] == "unsupported_file_type"


async def test_business_unit_default_entity(leorg: LeOrg) -> None:
    c = leorg.admin.client
    entity = await _entity(leorg, "BU Entity")
    bu = await c.post(
        f"{API}/admin/business-units",
        json={"name": "South", "default_legal_entity_id": entity["id"]},
        headers=h(leorg),
    )
    assert bu.status_code == 201, bu.text
    assert bu.json()["default_legal_entity_id"] == entity["id"]
    await c.post(f"{API}/legal-entities/{entity['id']}/deactivate", headers=h(leorg))
    refused = await c.patch(
        f"{API}/admin/business-units/{bu.json()['id']}",
        json={"default_legal_entity_id": entity["id"]},
        headers=h(leorg),
    )
    assert refused.status_code == 200  # unchanged value is accepted
    other = await c.post(
        f"{API}/admin/business-units",
        json={"name": "North", "default_legal_entity_id": entity["id"]},
        headers=h(leorg),
    )
    assert other.status_code == 422 and other.json()["error"]["code"] == "invalid_reference"


async def test_cross_tenant_404(leorg: LeOrg, other_leorg: LeOrg) -> None:
    c = other_leorg.admin.client
    eid = leorg.entity_id
    assert (await c.get(f"{API}/legal-entities/{eid}/readiness")).status_code == 404
    assert (await c.post(f"{API}/legal-entities/{eid}/reactivate", headers=h(other_leorg))).status_code == 404
    acct = (
        await leorg.admin.client.post(
            f"{API}/legal-entities/{eid}/bank-accounts",
            json={"label": "A", "bank_name": "B", "account_number": "12345678"},
            headers=h(leorg),
        )
    ).json()
    assert (
        await c.post(f"{API}/bank-accounts/{acct['id']}/unmask", headers=h(other_leorg))
    ).status_code == 404
    assert (
        await c.patch(f"{API}/bank-accounts/{acct['id']}", json={"label": "x"}, headers=h(other_leorg))
    ).status_code == 404


@pytest.mark.parametrize("kind", ["client_user", "vendor_user"])
async def test_external_sessions_refused(app: FastAPI, leorg: LeOrg, kind: str) -> None:
    client_id = await create_client(leorg.admin)
    if kind == "client_user":
        pid, org = await make_client_user(leorg.tenant_id, client_id), client_id
    else:
        org = await make_vendor(leorg.tenant_id)
        pid = await make_vendor_user(leorg.tenant_id, org)
    ext = await sign_in_external(app, kind, pid, leorg.tenant_id, org)
    try:
        for method, path in (
            ("get", f"{API}/legal-entities/{leorg.entity_id}/readiness"),
            ("post", f"{API}/legal-entities/{leorg.entity_id}/reactivate"),
            ("get", f"{API}/sso/identity-providers"),
        ):
            resp = await getattr(ext.client, method)(path, headers=ext.h())
            assert resp.status_code in (401, 403), (path, resp.status_code)
    finally:
        await ext.client.aclose()


async def test_finance_admin_role_template_seeded(leorg: LeOrg) -> None:
    async with tenant_session(leorg.tenant_id) as db:
        keys = set((await db.execute(select(Role.key))).scalars())
    assert {"sales", "hr_onboarding", "compliance", "finance", "finance_admin", "vendor_manager"} <= keys
