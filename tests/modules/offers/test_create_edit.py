"""Story 8.1: offer creation, prefill, one open offer per application, validation, masking, reads."""

import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI

from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import Member, join
from tests.modules.offers.helpers import (
    OFFERS,
    TO_SELECTED,
    add_application,
    create_candidate,
    create_offer,
    get_offer,
    move,
    patch,
    released_offer,
    setup_selected,
)


async def test_create_prefills_from_demand(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    assert offer["status"] == "draft"
    assert offer["human_id"].startswith("OFR-") and len(offer["human_id"]) == 10
    v = offer["current_version"]
    assert v["version_no"] == 1
    assert v["title"] == "Senior Data Engineer"
    assert v["employment_type"] == "contract"
    assert v["pay_rate"] == {"amount": "1100.00", "currency": "INR"}
    assert v["bill_rate"] == {"amount": "1500.00", "currency": "INR"}
    # margin = 1500 - 1100 * 1.18 = 202.00 -> 13.47 % (< 15 % minimum: a warning, not an error)
    assert v["margin_amount"] == {"amount": "202.00", "currency": "INR"}
    assert v["margin_pct"] == "13.47"
    assert v["below_min_margin"] is True and offer["warnings"] == ["below_min_margin"]
    assert v["end_date"] is not None and v["template_id"] is not None
    # Rendered by the (eager) task after commit.
    assert (await get_offer(dorg.admin, offer["id"]))["current_version"]["letter_ready"] is True
    assert date.fromisoformat(v["expiry_date"]) >= date.today() + timedelta(days=6)
    assert [x["to_status"] for x in offer["history"]] == ["draft"]
    assert offer["compensation_masked"] is False
    listed = (await dorg.admin.client.get(f"/api/v1/applications/{s.application_id}/offers")).json()
    assert [o["id"] for o in listed] == [offer["id"]]
    by_demand = (await dorg.admin.client.get(f"/api/v1/demands/{s.demand_id}/offers")).json()
    assert [o["id"] for o in by_demand] == [offer["id"]]
    page = (await dorg.admin.client.get(OFFERS, params={"owner": "mine", "status": "draft"})).json()
    assert offer["id"] in [o["id"] for o in page["items"]]


async def test_not_selected_is_409(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    cand = await create_candidate(dorg.admin, "Other Person")
    app_id = await add_application(dorg.admin, s.demand_id, cand)
    await move(dorg, app_id, *TO_SELECTED[:3])  # interviewing
    resp = await dorg.admin.client.post(
        f"/api/v1/applications/{app_id}/offers", json={}, headers=h(dorg.admin)
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "application_not_selected")


async def test_second_open_offer_is_409_and_allowed_after_decline(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    first = await create_offer(dorg.admin, s.application_id)
    resp = await dorg.admin.client.post(
        f"/api/v1/applications/{s.application_id}/offers", json={}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "offer_exists" and err["details"]["offer_id"] == first["id"]
    # Withdraw (terminal) -> a new offer is allowed.
    wd = await dorg.admin.client.post(
        f"{OFFERS}/{first['id']}/withdraw", json={"reason": "Rework the terms"}, headers=h(dorg.admin)
    )
    assert wd.status_code == 200 and wd.json()["status"] == "withdrawn"
    second = await create_offer(dorg.admin, s.application_id)
    assert second["id"] != first["id"]


async def test_new_offer_allowed_after_decline(dorg: DOrg) -> None:
    from app.core.database import tenant_session
    from app.services import offers_service

    s = await setup_selected(dorg)
    offer = await released_offer(s)
    async with tenant_session(dorg.tenant_id) as db:
        await offers_service.CandidateResponder(
            db, s.candidate_id, "portal", "203.0.113.4", "pytest"
        ).decline(uuid.UUID(offer["id"]), "Another offer", 1)
    again = await create_offer(dorg.admin, s.application_id)
    assert again["status"] == "draft"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"employment_type": "contract", "end_date": None}, "end_date_required"),
        ({"bill_rate": {"amount": "1500.00", "currency": "USD"}}, "currency_mismatch"),
        ({"expiry_date": (date.today() - timedelta(days=2)).isoformat()}, "expiry_in_past"),
    ],
)
async def test_validation(dorg: DOrg, body: dict[str, object], code: str) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    resp = await patch(dorg.admin, offer, **body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code


async def test_create_validation_422(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    resp = await dorg.admin.client.post(
        f"/api/v1/applications/{s.application_id}/offers",
        json={"employment_type": "contract", "start_date": "2030-01-10", "end_date": "2030-01-01"},
        headers=h(dorg.admin),
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "end_date_before_start")


async def test_masking_without_compensation_view(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    viewer: Member = await join(app, dorg.tenant, "viewer", name="Vic Viewer")
    try:
        resp = await viewer.client.get(f"{OFFERS}/{offer['id']}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        v = body["current_version"]
        assert body["compensation_masked"] is True
        assert v["pay_rate"]["amount"] == "1100.00"
        assert v["bill_rate"] is None and v["margin_amount"] is None and v["margin_pct"] is None
        assert v["burden_pct"] is None
        # Viewer cannot write.
        denied = await patch(viewer, offer, title="X")
        assert denied.status_code == 403
    finally:
        await viewer.client.aclose()


async def test_settings_roundtrip(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(f"{OFFERS}/settings")
    assert resp.status_code == 200
    assert resp.json()["default_expiry_days"] == 7
    upd = await dorg.admin.client.patch(
        f"{OFFERS}/settings",
        json={
            "default_expiry_days": 10,
            "signatory_name": "Meera Iyer",
            "signatory_title": "Head of Delivery",
        },
        headers=h(dorg.admin),
    )
    assert upd.status_code == 200 and upd.json()["signatory_name"] == "Meera Iyer"
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    assert date.fromisoformat(offer["current_version"]["expiry_date"]) >= date.today() + timedelta(days=9)


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    other = other_dorg.admin
    oid = offer["id"]
    for method, url, body in (
        ("GET", f"{OFFERS}/{oid}", None),
        ("PATCH", f"{OFFERS}/{oid}", {"lock_version": 1, "title": "x"}),
        ("POST", f"{OFFERS}/{oid}/submit", None),
        ("POST", f"{OFFERS}/{oid}/release", None),
        ("POST", f"{OFFERS}/{oid}/withdraw", {"reason": "not mine at all"}),
        ("GET", f"{OFFERS}/{oid}/versions/1", None),
        ("POST", f"{OFFERS}/{oid}/letter/preview", None),
        ("GET", f"{OFFERS}/{oid}/letter", None),
        ("GET", f"{OFFERS}/{oid}/accepted-letter", None),
        ("GET", f"/api/v1/applications/{s.application_id}/offers", None),
        ("POST", f"/api/v1/applications/{s.application_id}/offers", {}),
        ("GET", f"/api/v1/demands/{s.demand_id}/offers", None),
    ):
        resp = await other.client.request(method, url, json=body, headers=h(other))
        assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
    page = (await other.client.get(OFFERS)).json()
    assert oid not in [o["id"] for o in page["items"]]
