"""Billing profile (FR-88), payment terms and due-date precedence (Epic 16 A1-A2)."""

import uuid
from datetime import date

from app.core.database import tenant_session
from app.services import commercials_service
from tests.modules.commercials.helpers import API, COrg, create_contract, err, ok
from tests.modules.legal_entities.conftest import gstin
from tests.modules.pipeline.helpers import create_contact

PAN = "ABCDE1234F"


async def profile(org: COrg, **overrides: object) -> dict:
    body = {"legal_name": "Acme India Pvt Ltd", "pan": PAN, "tax_treatment": "registered", **overrides}
    return await ok(
        await org.c.put(f"{API}/clients/{org.client_id}/billing-profile", json=body, headers=org.h())
    )


def address(**overrides: object) -> dict:
    body = {
        "label": "HQ",
        "line1": "1 MG Road",
        "city": "Bengaluru",
        "state_code": "29",
        "postal_code": "560001",
        "gstin": gstin("29", PAN),
    }
    body.update(overrides)
    return body


async def test_profile_address_gstin_rules(corg: COrg) -> None:
    assert (await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/billing-profile"))) is None
    await profile(corg)
    url = f"{API}/clients/{corg.client_id}/billing-addresses"
    err(await corg.c.post(url, json=address(gstin=None), headers=corg.h()), 422, "gstin_required")
    bad = err(
        await corg.c.post(url, json=address(gstin=gstin("27", PAN)), headers=corg.h()), 422, "gstin_invalid"
    )
    assert bad["details"]["reason"] == "state_mismatch"
    err(await corg.c.post(url, json=address(state_code=None), headers=corg.h()), 422, "state_required")
    first = await ok(await corg.c.post(url, json=address(), headers=corg.h()), 201)
    assert first["is_default"] is True
    # The last active address cannot be archived.
    err(
        await corg.c.post(f"{API}/billing-addresses/{first['id']}/archive", headers=corg.h()),
        409,
        "billing_address_required",
    )
    second = await ok(
        await corg.c.post(url, json=address(label="Branch", is_default=True), headers=corg.h()), 201
    )
    full = await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/billing-profile"))
    assert full["complete"] is True
    assert [a["id"] for a in full["addresses"] if a["is_default"]] == [second["id"]]
    archived = await ok(
        await corg.c.post(f"{API}/billing-addresses/{second['id']}/archive", headers=corg.h())
    )
    assert archived["archived_at"] is not None
    full = await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/billing-profile"))
    assert [a["id"] for a in full["addresses"] if a["is_default"]] == [first["id"]]


async def test_unregistered_export_and_sez(corg: COrg) -> None:
    await profile(corg, tax_treatment="unregistered")
    url = f"{API}/clients/{corg.client_id}/billing-addresses"
    await ok(await corg.c.post(url, json=address(gstin=None), headers=corg.h()), 201)
    # Switching to SEZ re-validates the existing address (not SEZ).
    err(
        await corg.c.put(
            f"{API}/clients/{corg.client_id}/billing-profile",
            json={"legal_name": "Acme", "pan": PAN, "tax_treatment": "sez"},
            headers=corg.h(),
        ),
        422,
        "gstin_required",
    )
    err(
        await corg.c.put(
            f"{API}/clients/{corg.client_id}/billing-profile",
            json={"legal_name": "Acme", "pan": PAN, "tax_treatment": "export"},
            headers=corg.h(),
        ),
        422,
        "export_requires_foreign_address",
    )


async def test_export_client_needs_foreign_address(corg: COrg) -> None:
    await profile(corg, tax_treatment="export", pan=None)
    url = f"{API}/clients/{corg.client_id}/billing-addresses"
    err(
        await corg.c.post(url, json=address(gstin=None), headers=corg.h()),
        422,
        "export_requires_foreign_address",
    )
    await ok(
        await corg.c.post(
            url, json=address(country="US", state_code=None, gstin=None, city="Austin"), headers=corg.h()
        ),
        201,
    )


async def test_contacts_and_facts(corg: COrg) -> None:
    contact_id, email = await create_contact(corg.admin, corg.client_id)
    await profile(corg)
    await ok(
        await corg.c.post(
            f"{API}/clients/{corg.client_id}/billing-addresses", json=address(), headers=corg.h()
        ),
        201,
    )
    err(
        await corg.c.put(
            f"{API}/clients/{corg.client_id}/billing-profile/contacts",
            json={"to": [str(uuid.uuid4())]},
            headers=corg.h(),
        ),
        422,
        "contact_invalid",
    )
    out = await ok(
        await corg.c.put(
            f"{API}/clients/{corg.client_id}/billing-profile/contacts",
            json={"to": [str(contact_id)]},
            headers=corg.h(),
        )
    )
    assert out["contacts"][0]["role"] == "to"
    async with tenant_session(corg.tenant_id) as db:
        facts = await commercials_service.get_billing_profile(corg.ctx(), db, corg.client_id)
        await commercials_service.assert_invoiceable(corg.ctx(), db, corg.client_id)
    assert facts.billing_contacts.to == [email.lower()]
    assert facts.tax_treatment == "registered" and facts.addresses[0].state_code == "29"
    assert facts.payment_terms_days == 30  # tenant default (Net 30)


async def test_invoiceable_requires_address(corg: COrg) -> None:
    import pytest

    from app.core.errors import ConflictError

    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.assert_invoiceable(corg.ctx(), db, corg.client_id)
    assert exc.value.code == "billing_profile_incomplete"


async def test_payment_terms_and_due_date_precedence(corg: COrg) -> None:
    terms = await ok(await corg.c.get(f"{API}/payment-terms"))
    assert [t["name"] for t in terms] == ["Due on receipt", "Net 30", "Net 45", "Net 60"]
    default = next(t for t in terms if t["is_default"])
    err(
        await corg.c.post(f"{API}/payment-terms/{default['id']}/archive", headers=corg.h()),
        409,
        "payment_terms_default",
    )
    net15 = await ok(
        await corg.c.post(f"{API}/payment-terms", json={"name": "Net 15", "days": 15}, headers=corg.h()), 201
    )
    net45 = next(t for t in terms if t["days"] == 45)
    contract = await create_contract(corg, payment_terms_id=net15["id"])
    doc = date(2026, 10, 1)
    async with tenant_session(corg.tenant_id) as db:
        assert await commercials_service.due_date(corg.ctx(), db, corg.client_id, None, doc) == date(
            2026, 10, 31
        )
    await profile(corg, payment_terms_id=net45["id"])
    async with tenant_session(corg.tenant_id) as db:
        assert await commercials_service.due_date(corg.ctx(), db, corg.client_id, None, doc) == date(
            2026, 11, 15
        )
        assert await commercials_service.due_date(
            corg.ctx(), db, corg.client_id, uuid.UUID(contract["id"]), doc
        ) == date(2026, 10, 16)
    made_default = await ok(
        await corg.c.patch(f"{API}/payment-terms/{net15['id']}", json={"is_default": True}, headers=corg.h())
    )
    assert made_default["is_default"] is True
    terms = await ok(await corg.c.get(f"{API}/payment-terms"))
    assert [t["name"] for t in terms if t["is_default"]] == ["Net 15"]
