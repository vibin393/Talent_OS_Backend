"""Story 8.1: merge-field whitelist, template CRUD, letter PDF content (never bill rate / margin)."""

import uuid

import pytest

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.services.offers import letter_pdf, templates, versions
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import OFFERS, create_offer, get_offer, setup_selected, submit

TEMPLATES = "/api/v1/offer-templates"


@pytest.mark.parametrize(
    "field", ["offer.bill_rate", "offer.margin_pct", "offer.burden_pct", "candidate.email", "secret"]
)
def test_disallowed_fields_rejected(field: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        templates.validate_template(f"Hello {{{{ {field} }}}}")
    assert info.value.code == "unknown_merge_field"
    assert info.value.details["field"] == field


def test_every_whitelisted_field_renders() -> None:
    body = "\n".join(f"{{{{ {k} }}}}" for k in templates.MERGE_FIELDS)
    out = templates.validate_template(body)
    assert "Asha Rao" in out


def test_sandbox_blocks_attribute_tricks() -> None:
    with pytest.raises(ValidationFailed):
        templates.validate_template("{{ offer.title.__class__ }}")


async def test_template_api(dorg: DOrg) -> None:
    listed = (await dorg.admin.client.get(TEMPLATES)).json()
    assert {t for row in listed for t in row["employment_types"]} >= {"contract", "permanent"}
    fields = (await dorg.admin.client.get(f"{TEMPLATES}/merge-fields")).json()
    assert "offer.bill_rate" not in {f["key"] for f in fields}
    bad = await dorg.admin.client.post(
        TEMPLATES,
        json={"name": "Bad", "employment_types": ["permanent"], "body_md": "{{ offer.bill_rate }}"},
        headers=h(dorg.admin),
    )
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "unknown_merge_field")
    assert bad.json()["error"]["details"]["field"] == "offer.bill_rate"
    ok = await dorg.admin.client.post(
        TEMPLATES,
        json={
            "name": "Perm v2",
            "employment_types": ["permanent"],
            "body_md": "# Hi {{ candidate.full_name }}",
            "is_default": True,
        },
        headers=h(dorg.admin),
    )
    assert ok.status_code == 201, ok.text
    upd = await dorg.admin.client.patch(
        f"{TEMPLATES}/{ok.json()['id']}",
        json={"body_md": "# Hello {{ candidate.first_name }}"},
        headers=h(dorg.admin),
    )
    assert upd.status_code == 200 and upd.json()["version"] == 2
    preview = await dorg.admin.client.post(
        f"{TEMPLATES}/preview", json={"body_md": "# {{ offer.title }}"}, headers=h(dorg.admin)
    )
    assert preview.status_code == 200 and preview.headers["content-type"] == "application/pdf"
    assert "Senior Data Engineer" in letter_pdf.pdf_text(preview.content)


async def test_letter_contains_name_and_pay_never_bill_or_margin(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(
        dorg.admin, s.application_id, bill_rate={"amount": "1987.00", "currency": "INR"}
    )
    resp = await dorg.admin.client.post(f"{OFFERS}/{offer['id']}/letter/preview", headers=h(dorg.admin))
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf"
    text = letter_pdf.pdf_text(resp.content)
    assert "Asha Rao" in text and "INR 1,100.00" in text
    assert "1,987" not in text and "1987" not in text
    margin = offer["current_version"]["margin_amount"]["amount"]
    assert margin.split(".")[0] not in text.replace(",", "")
    # Stored letter (by the task) has the same properties and redirects to the files download.
    redirect = await dorg.admin.client.get(f"{OFFERS}/{offer['id']}/letter", follow_redirects=False)
    assert redirect.status_code == 302 and "/api/v1/files/" in redirect.headers["location"]
    pdf = await dorg.admin.client.get(redirect.headers["location"])
    assert pdf.status_code == 200 and "Asha Rao" in letter_pdf.pdf_text(pdf.content)


async def test_frozen_version_not_rerendered(dorg: DOrg) -> None:
    from app.models.offers import Offer

    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    await submit(dorg.admin, offer["id"])
    before = (await get_offer(dorg.admin, offer["id"]))["current_version"]
    assert before["frozen"] is True and before["letter_file_id"]
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(Offer, uuid.UUID(offer["id"]))
        assert row is not None
        version = await versions.current(db, row)
        from app.core.context import RequestContext

        assert (
            await versions.ensure_letter(RequestContext(tenant_id=dorg.tenant_id), db, row, version) is False
        )
    after = (await get_offer(dorg.admin, offer["id"]))["current_version"]
    assert after["letter_file_id"] == before["letter_file_id"]
