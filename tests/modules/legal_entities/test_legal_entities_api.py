"""Story 13.2: Legal Entities, GST registrations, bank accounts, series and FX over the API (AD-17)."""

import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.models.identity import Tenant
from app.models.legal_entities import AccountingPeriod, LegalEntity, LegalEntityBankAccount
from app.models.privacy import RetentionClass
from app.services import legal_entities_service
from tests.modules.identity.conftest import csrf, join, tenant_body
from tests.modules.legal_entities.conftest import API, LeOrg, gstin


def h(org: LeOrg) -> dict[str, str]:
    return csrf(org.admin.client)


async def _create(org: LeOrg, name: str = "Second Entity", **extra: object) -> dict[str, object]:
    body = {"name": name, "legal_name": f"{name} Pvt Ltd", "pan": "ABCDE1234F", "state_code": "29", **extra}
    resp = await org.admin.client.post(f"{API}/legal-entities", json=body, headers=h(org))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_crud_and_provisioned_default(leorg: LeOrg) -> None:
    resp = await leorg.admin.client.get(f"{API}/legal-entities")
    assert resp.status_code == 200
    [default] = resp.json()
    assert default["id"] == str(leorg.entity_id)
    assert default["is_backfilled"] is True and default["status"] == "active"
    assert default["default_currency"] == "INR" and default["timezone"] == "Asia/Kolkata"

    created = await _create(leorg)
    got = await leorg.admin.client.get(f"{API}/legal-entities/{created['id']}")
    assert got.json()["legal_name"] == "Second Entity Pvt Ltd"
    patched = await leorg.admin.client.patch(
        f"{API}/legal-entities/{created['id']}", json={"signatory_name": "A. Signer"}, headers=h(leorg)
    )
    assert patched.status_code == 200 and patched.json()["signatory_name"] == "A. Signer"
    dup = await leorg.admin.client.post(
        f"{API}/legal-entities", json={"name": "Second Entity", "legal_name": "x"}, headers=h(leorg)
    )
    assert dup.status_code == 409
    bad_ccy = await leorg.admin.client.post(
        f"{API}/legal-entities", json={"name": "K", "legal_name": "K", "default_currency": "KWD"}, headers=h(leorg)
    )
    assert bad_ccy.status_code == 422 and bad_ccy.json()["error"]["code"] == "currency_not_supported"
    # Periods for the new entity: current month through + 2.
    periods = await leorg.admin.client.get(f"{API}/legal-entities/{created['id']}/periods")
    assert len(periods.json()) == 3 and {p["status"] for p in periods.json()} == {"open"}


async def test_cross_tenant_is_404(leorg: LeOrg, other_leorg: LeOrg) -> None:
    other = other_leorg.entity_id
    c = leorg.admin.client
    assert (await c.get(f"{API}/legal-entities/{other}")).status_code == 404
    assert (await c.patch(f"{API}/legal-entities/{other}", json={"cin": "X"}, headers=h(leorg))).status_code == 404
    assert (await c.delete(f"{API}/legal-entities/{other}", headers=h(leorg))).status_code == 404
    assert (await c.get(f"{API}/legal-entities/{other}/gst-registrations")).status_code == 404
    assert (await c.get(f"{API}/legal-entities/{other}/bank-accounts")).status_code == 404
    assert (await c.get(f"{API}/legal-entities/{other}/periods")).status_code == 404


async def test_permissions(app: FastAPI, leorg: LeOrg) -> None:
    recruiter = await join(app, leorg.tenant, "recruiter")
    try:
        assert (await recruiter.client.get(f"{API}/legal-entities")).status_code == 403
        resp = await recruiter.client.post(f"{API}/fx-rates", json={}, headers=csrf(recruiter.client))
        assert resp.status_code in (403, 422)
    finally:
        await recruiter.client.aclose()


async def test_gst_registrations_validate_gstin(leorg: LeOrg) -> None:
    created = await _create(leorg, "GST Co")
    url = f"{API}/legal-entities/{created['id']}/gst-registrations"
    good = gstin("29", "ABCDE1234F")
    bad_check = good[:14] + ("A" if good[14] != "A" else "B")
    resp = await leorg.admin.client.post(url, json={"gstin": bad_check, "state_code": "29"}, headers=h(leorg))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gstin_checksum"
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "gstin"
    mismatch = await leorg.admin.client.post(url, json={"gstin": good, "state_code": "33"}, headers=h(leorg))
    assert mismatch.json()["error"]["code"] == "gstin_state_mismatch"
    other_pan = gstin("29", "ZZZZZ9999Z")
    pan = await leorg.admin.client.post(url, json={"gstin": other_pan, "state_code": "29"}, headers=h(leorg))
    assert pan.json()["error"]["code"] == "gstin_pan_mismatch"
    ok = await leorg.admin.client.post(url, json={"gstin": good, "state_code": "29"}, headers=h(leorg))
    assert ok.status_code == 201, ok.text
    assert ok.json()["is_principal"] is True  # the first registration becomes principal
    second = await leorg.admin.client.post(url, json={"gstin": gstin("33", "ABCDE1234F"), "state_code": "33"}, headers=h(leorg))
    assert second.status_code == 201 and second.json()["is_principal"] is False
    lut = await leorg.admin.client.post(
        f"{API}/gst-registrations/{ok.json()['id']}/luts",
        json={"fiscal_year": "26-27", "lut_number": "AD290426000001", "valid_from": "2026-04-01", "valid_to": "2027-03-31"},
        headers=h(leorg),
    )
    assert lut.status_code == 201
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        uid = uuid.UUID(created["id"])  # type: ignore[arg-type]
        assert await legal_entities_service.default_registration_for(ctx, db, uid, "33") == uuid.UUID(second.json()["id"])
        assert await legal_entities_service.default_registration_for(ctx, db, uid, "07") == uuid.UUID(ok.json()["id"])
        assert (await legal_entities_service.get_lut(ctx, db, uuid.UUID(ok.json()["id"]), "26-27")) is not None
        assert (await legal_entities_service.get_gst_registration(ctx, db, uuid.UUID(ok.json()["id"]))).gstin == good


async def test_bank_accounts_are_masked(leorg: LeOrg) -> None:
    url = f"{API}/legal-entities/{leorg.entity_id}/bank-accounts"
    resp = await leorg.admin.client.post(
        url, json={"label": "Main", "bank_name": "HDFC", "account_number": "50100123456789", "ifsc": "HDFC0001234"}, headers=h(leorg)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["account_number_last4"] == "6789" and body["is_default"] is True
    assert "account_number" not in body
    listed = await leorg.admin.client.get(url)
    assert "50100123456789" not in listed.text
    async with tenant_session(leorg.tenant_id) as db:
        raw = (await db.execute(text("SELECT account_number FROM legal_entity_bank_accounts WHERE id = :id"), {"id": body["id"]})).scalar_one()
        assert "50100123456789" not in raw
        doc = await legal_entities_service.bank_account_for_documents(RequestContext(tenant_id=leorg.tenant_id), db, leorg.entity_id, "INR")
        assert doc.account_number == "50100123456789"


async def test_series_config_and_preview(leorg: LeOrg) -> None:
    entity = await _create(leorg, "Series Co")
    reg = await leorg.admin.client.post(
        f"{API}/legal-entities/{entity['id']}/gst-registrations", json={"gstin": gstin("29"), "state_code": "29"}, headers=h(leorg)
    )
    url = f"{API}/legal-entities/{entity['id']}/document-series"
    no_reg = await leorg.admin.client.post(url, json={"doc_type": "invoice", "name": "Inv"}, headers=h(leorg))
    assert no_reg.json()["error"]["code"] == "series_registration_required"
    too_long = await leorg.admin.client.post(
        url,
        json={"doc_type": "invoice", "name": "Inv", "gst_registration_id": reg.json()["id"], "prefix": "ABCDEFGHIJ", "fy_format": "YYYY-YY", "pad": 5},
        headers=h(leorg),
    )
    assert too_long.status_code == 422 and too_long.json()["error"]["code"] == "series_too_long"
    bad_chars = await leorg.admin.client.post(
        url, json={"doc_type": "invoice", "name": "Inv", "gst_registration_id": reg.json()["id"], "prefix": "IN#"}, headers=h(leorg)
    )
    assert bad_chars.json()["error"]["code"] == "series_bad_chars"
    ok = await leorg.admin.client.post(
        url, json={"doc_type": "invoice", "name": "Inv", "gst_registration_id": reg.json()["id"], "prefix": "INV"}, headers=h(leorg)
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["next_number_preview"].startswith("INV/")
    dup = await leorg.admin.client.post(
        url, json={"doc_type": "invoice", "name": "Inv2", "gst_registration_id": reg.json()["id"]}, headers=h(leorg)
    )
    assert dup.status_code == 409
    patched = await leorg.admin.client.patch(f"{API}/document-series/{ok.json()['id']}", json={"prefix": "IV"}, headers=h(leorg))
    assert patched.status_code == 200 and patched.json()["prefix"] == "IV"
    async with tenant_session(leorg.tenant_id) as db:
        sid = await legal_entities_service.series_for(
            RequestContext(tenant_id=leorg.tenant_id), db,
            legal_entity_id=uuid.UUID(entity["id"]), gst_registration_id=uuid.UUID(reg.json()["id"]), doc_type="invoice",  # type: ignore[arg-type]
        )
        assert sid == uuid.UUID(ok.json()["id"])
        with pytest.raises(ConflictError) as exc:
            await legal_entities_service.series_for(
                RequestContext(tenant_id=leorg.tenant_id), db,
                legal_entity_id=uuid.UUID(entity["id"]), gst_registration_id=None, doc_type="credit_note",  # type: ignore[arg-type]
            )
        assert exc.value.code == "series_missing"


async def test_delete_in_use_and_last_active(leorg: LeOrg) -> None:
    only = await leorg.admin.client.delete(f"{API}/legal-entities/{leorg.entity_id}", headers=h(leorg))
    assert only.status_code == 409 and only.json()["error"]["code"] == "legal_entity_in_use"
    entity = await _create(leorg, "Busy Co")
    client = await leorg.admin.client.post(f"{API}/clients", json={"name": f"C {uuid.uuid4().hex[:6]}"}, headers=h(leorg))
    async with tenant_session(leorg.tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO contracts (id, tenant_id, human_id, contract_type, client_id, legal_entity_id, start_date, currency) "
                "VALUES (:id, :t, 'CON-0001', 'msa', :c, :le, '2026-04-01', 'INR')"
            ),
            {"id": uuid.uuid4(), "t": leorg.tenant_id, "c": client.json()["id"], "le": entity["id"]},
        )
    busy = await leorg.admin.client.delete(f"{API}/legal-entities/{entity['id']}", headers=h(leorg))
    assert busy.status_code == 409
    assert busy.json()["error"]["details"]["tables"] == ["contracts"]
    free = await _create(leorg, "Free Co")
    assert (await leorg.admin.client.delete(f"{API}/legal-entities/{free['id']}", headers=h(leorg))).status_code == 204
    assert (await leorg.admin.client.get(f"{API}/legal-entities/{free['id']}")).status_code == 404


async def test_deactivate_and_document_acceptance(leorg: LeOrg) -> None:
    last = await leorg.admin.client.post(f"{API}/legal-entities/{leorg.entity_id}/deactivate", headers=h(leorg))
    assert last.status_code == 409
    entity = await _create(leorg, "Short Lived")
    off = await leorg.admin.client.post(f"{API}/legal-entities/{entity['id']}/deactivate", headers=h(leorg))
    assert off.status_code == 200 and off.json()["status"] == "inactive"
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await legal_entities_service.assert_accepts_new_documents(ctx, db, uuid.UUID(entity["id"]))  # type: ignore[arg-type]
        assert exc.value.code == "legal_entity_inactive"
        await legal_entities_service.assert_accepts_new_documents(ctx, db, leorg.entity_id)
        with pytest.raises(ValidationFailed) as incomplete:
            await legal_entities_service.assert_accepts_new_documents(ctx, db, leorg.entity_id, require_complete=True)
        assert incomplete.value.code == "legal_entity_incomplete"
        # Epic 14 extends the rule with the default bank account and the signatory.
        assert set(incomplete.value.details["missing"]) == {
            "pan",
            "state_code",
            "principal_gst_registration",
            "bank_account",
            "signatory",
        }
    on = await leorg.admin.client.post(f"{API}/legal-entities/{entity['id']}/activate", headers=h(leorg))
    assert on.json()["status"] == "active"


async def test_fx_rates(leorg: LeOrg) -> None:
    resp = await leorg.admin.client.post(
        f"{API}/fx-rates", json={"from_currency": "USD", "to_currency": "INR", "rate_date": "2026-09-25", "rate": "83.1234567", "source": "RBI"}, headers=h(leorg)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["rate"] == "83.123457"
    no_source = await leorg.admin.client.post(
        f"{API}/fx-rates", json={"from_currency": "USD", "to_currency": "INR", "rate_date": "2026-09-26", "rate": "83", "source": ""}, headers=h(leorg)
    )
    assert no_source.status_code == 422
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        exact = await legal_entities_service.rate_for(ctx, db, "USD", "INR", date(2026, 9, 25))
        assert str(exact.rate) == "83.123457" and exact.source == "RBI"
        later = await legal_entities_service.rate_for(ctx, db, "USD", "INR", date(2026, 10, 2))
        assert later.rate_date == date(2026, 9, 25)
        with pytest.raises(ValidationFailed) as missing:
            await legal_entities_service.rate_for(ctx, db, "USD", "INR", date(2026, 10, 3))
        assert missing.value.code == "fx_rate_missing"
        assert (await legal_entities_service.rate_for(ctx, db, "EUR", "EUR", date(2026, 1, 1))).rate == 1
    listed = await leorg.admin.client.get(f"{API}/fx-rates?from_currency=USD")
    assert [r["rate"] for r in listed.json()] == ["83.123457"]


async def test_backfill_shape() -> None:
    """Migration 200_02 gave every pre-existing tenant one backfilled entity, BU defaults and open periods."""
    async with platform_session() as pdb:
        tenant_ids = list((await pdb.execute(select(Tenant.id).where(Tenant.status != "deleted").limit(3))).scalars())
    for tid in tenant_ids:
        async with tenant_session(tid) as db:
            entities = list((await db.execute(select(LegalEntity))).scalars())
            if not entities:
                continue  # tenants created by tests after the migration without provisioning
            assert any(e.is_backfilled for e in entities)
            nulls = (await db.execute(text("SELECT count(*) FROM business_units WHERE default_legal_entity_id IS NULL"))).scalar_one()
            assert nulls == 0
            periods = (await db.execute(select(AccountingPeriod))).scalars().all()
            assert periods and all(p.status in ("open", "closed") for p in periods)


async def test_operator_provisioning_creates_entity_periods_and_retention(op_client: httpx.AsyncClient) -> None:
    resp = await op_client.post("/api/v1/platform/tenants", json=tenant_body(), headers=csrf(op_client))
    assert resp.status_code == 201, resp.text
    tenant_id = uuid.UUID(resp.json()["id"])
    async with tenant_session(tenant_id) as db:
        entities = list((await db.execute(select(LegalEntity))).scalars())
        assert len(entities) == 1 and entities[0].name == "Acme Staffing" and entities[0].status == "active"
        periods = (await db.execute(select(AccountingPeriod).where(AccountingPeriod.legal_entity_id == entities[0].id))).scalars().all()
        assert len(periods) == 3
        keys = set((await db.execute(select(RetentionClass.key))).scalars())
        # Epic 13 classes; later Phase 2 epics seed more (worker_profile, timesheets_unbilled).
        assert keys >= {"financial", "signed_agreements", "timesheets", "bgv_evidence"}
        bgv = (await db.execute(select(RetentionClass).where(RetentionClass.key == "bgv_evidence"))).scalar_one()
        # Epic 35 (A3) re-models the class: a 12-month period (``min_retention_months``) with a 12-month floor.
        assert (bgv.min_retention_months, bgv.delete_after_months, bgv.floor_months) == (12, None, 12)
        assert (await db.execute(select(LegalEntityBankAccount))).first() is None
