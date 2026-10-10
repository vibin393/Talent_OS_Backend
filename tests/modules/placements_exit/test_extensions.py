# ruff: noqa: E501
"""Extensions: propose / checks / approvals / client confirmation (portal, Secure Link, recorded) / confirm."""

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.events import subscribe, unsubscribe
from app.events.placements import ExtensionConfirmed, RateSegmentAdded
from app.schemas.commercials import PoValidity
from app.services import commercials_service
from tests.modules.demands.helpers import DOrg
from tests.modules.placements_exit.helpers import API, PL, active, placement_row, post, sql
from tests.p2 import make_client_user, sign_in_external


def _new_end(days: int = 120) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


async def _propose(org: DOrg, pid: str, **body: Any) -> Any:
    return await post(org, f"/placements/{pid}/extensions", {"new_end_date": _new_end(), **body})


async def test_propose_without_exceptions_goes_to_client(dorg: DOrg) -> None:
    p = await active(dorg)
    resp = await _propose(dorg, p["id"])
    assert resp.status_code == 201, resp.text
    ext = resp.json()
    assert ext["status"] == "pending_client_confirmation" and ext["human_id"].startswith("EXT")
    names = {c["name"]: c["status"] for c in ext["checks"]}
    assert names["offboarding_in_progress"] == "pass" and names["credentials"] == "pass"
    again = await _propose(dorg, p["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "extension_exists"


async def test_preview_and_masking(dorg: DOrg) -> None:
    p = await active(dorg)
    resp = await post(
        dorg,
        f"/placements/{p['id']}/extensions/preview",
        {"new_end_date": _new_end(), "pay_rate": "1000", "bill_rate": "1600"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blocking"] is False and body["margin_amount"] is not None
    bad = await post(
        dorg, f"/placements/{p['id']}/extensions/preview", {"new_end_date": date.today().isoformat()}
    )
    assert bad.status_code == 422


async def test_confirm_updates_end_date_and_adds_segment(dorg: DOrg) -> None:
    p = await active(dorg)
    seen: list[Any] = []
    subscribe(RateSegmentAdded, seen.append)
    subscribe(ExtensionConfirmed, seen.append)
    try:
        ext = (await _propose(dorg, p["id"], pay_rate="1100.00", bill_rate="1700.00")).json()
        assert ext["status"] == "pending_client_confirmation", ext
        resp = await post(dorg, f"/extensions/{ext['id']}/confirm", {"send_extension_letter": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (
            body["status"] == "confirmed"
            and body["rate_segment_id"]
            and body["send_extension_letter"] is True
        )
    finally:
        unsubscribe(RateSegmentAdded, seen.append)
        unsubscribe(ExtensionConfirmed, seen.append)
    row = await placement_row(dorg, p["id"])
    assert row["end_date"].isoformat() == _new_end()
    hist = await sql(
        dorg, "SELECT cause FROM placement_end_date_history WHERE placement_id = :i", i=uuid.UUID(p["id"])
    )
    assert [h["cause"] for h in hist] == ["extension"]
    assert {type(e).__name__ for e in seen} == {"RateSegmentAdded", "ExtensionConfirmed"}


async def test_expired_credential_blocks_until_exception_approved(dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    await sql(dorg, "UPDATE placements SET compliance_blocked = true WHERE id = :i", i=uuid.UUID(p["id"]))
    resp = await post(dorg, f"/extensions/{ext['id']}/confirm", {})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "extension_blocked", resp.text
    resp = await post(dorg, f"/extensions/{ext['id']}/recheck")
    assert resp.status_code == 200
    resp = await post(dorg, f"/extensions/{ext['id']}/request-exception", {"reason": "Renewal in progress"})
    assert resp.status_code == 200, resp.text
    req = resp.json()["exception_approval_request_id"]
    if resp.json()["exception_status"] == "pending":
        d = await post(dorg, f"/approvals/{req}/decide", {"action": "approve"})
        assert d.status_code == 200, d.text
    resp = await post(dorg, f"/extensions/{ext['id']}/confirm", {})
    assert resp.status_code == 200, resp.text


async def test_po_not_covering_requires_approval_then_passes(
    dorg: DOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def invalid(*_: Any, **__: Any) -> PoValidity:
        return PoValidity(valid=False, reason="po_expired", remaining_amount=None, remaining_hours=None)

    monkeypatch.setattr(commercials_service, "validate_po", invalid)
    p = await active(dorg)
    resp = await _propose(dorg, p["id"], po_id=str(uuid.uuid4()))
    assert resp.status_code == 201, resp.text
    ext = resp.json()
    assert {c["name"]: c["status"] for c in ext["checks"]}["po_validity"] == "fail"
    if ext["status"] == "pending_approval":
        d = await post(dorg, f"/approvals/{ext['approval_request_id']}/decide", {"action": "approve"})
        assert d.status_code == 200, d.text
        ext = (await dorg.admin.client.get(f"{API}/extensions/{ext['id']}")).json()
    assert ext["status"] == "pending_client_confirmation"
    assert (await post(dorg, f"/extensions/{ext['id']}/confirm", {})).status_code == 200


async def test_contract_end_warning_creates_renewal_task(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.placements import extension_checks

    async def warn(*_: Any, **__: Any) -> Any:
        return extension_checks._r("contract_end", "warn", contract_end=date.today().isoformat())

    monkeypatch.setattr(extension_checks, "_contract_end", warn)
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    assert (await post(dorg, f"/extensions/{ext['id']}/confirm", {})).status_code == 200
    tasks = await sql(
        dorg, "SELECT title FROM tasks WHERE source = 'offboarding' AND title LIKE 'Renew contract%'"
    )
    assert len(tasks) == 1


async def test_recorded_response_needs_evidence(dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    resp = await post(dorg, f"/extensions/{ext['id']}/client-response", {"decision": "confirm"})
    assert resp.status_code == 422


async def test_withdraw_then_client_response_conflict(app: FastAPI, dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    assert (
        await post(dorg, f"/extensions/{ext['id']}/withdraw", {"reason": "Client changed mind"})
    ).status_code == 200
    cu = await make_client_user(dorg.tenant_id, dorg.client_id, permissions=("placements:view",))
    client = await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)
    resp = await client.client.post(
        f"{API}/client-portal/placements/{p['id']}/extensions/{ext['id']}/respond",
        json={"decision": "confirm"},
        headers=client.h(),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "extension_not_pending", resp.text


async def test_client_portal_confirm_and_scope(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"], pay_rate="1100.00", bill_rate="1700.00")).json()
    cu = await make_client_user(dorg.tenant_id, dorg.client_id, permissions=("placements:view",))
    client = await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)
    listing = await client.client.get(f"{API}/client-portal/placements/extensions")
    assert listing.status_code == 200, listing.text
    (row,) = [r for r in listing.json() if r["id"] == ext["id"]]
    assert not ({"pay_rate", "bill_rate", "vendor_rate", "margin_amount", "margin_pct"} & set(row))
    # a restricted Client User without scope sees nothing and gets 404
    ru = await make_client_user(
        dorg.tenant_id, dorg.client_id, permissions=("placements:view",), restricted=True
    )
    restricted = await sign_in_external(app, "client_user", ru, dorg.tenant_id, dorg.client_id)
    resp = await restricted.client.post(
        f"{API}/client-portal/placements/{p['id']}/extensions/{ext['id']}/respond",
        json={"decision": "confirm"},
        headers=restricted.h(),
    )
    assert resp.status_code == 404
    # a Client User of another tenant's client gets 404
    ou = await make_client_user(other_dorg.tenant_id, other_dorg.client_id, permissions=("placements:view",))
    other = await sign_in_external(app, "client_user", ou, other_dorg.tenant_id, other_dorg.client_id)
    resp = await other.client.post(
        f"{API}/client-portal/placements/{p['id']}/extensions/{ext['id']}/respond",
        json={"decision": "confirm"},
        headers=other.h(),
    )
    assert resp.status_code == 404
    resp = await client.client.post(
        f"{API}/client-portal/placements/{p['id']}/extensions/{ext['id']}/respond",
        json={"decision": "confirm", "comment": "Approved"},
        headers=client.h(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    assert (await placement_row(dorg, p["id"]))["end_date"].isoformat() == _new_end()


async def test_secure_link_get_never_mutates_and_double_post_410(dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    link = await post(dorg, f"/extensions/{ext['id']}/client-link")
    assert link.status_code == 200, link.text
    token = link.json()["url"].rsplit("/", 1)[-1]
    anon = dorg.admin.client
    for _ in range(2):
        view = await anon.get(f"{API}/public/links/{token}/extension")
        assert view.status_code == 200 and view.json()["status"] == "pending_client_confirmation"
    frame = await anon.get(f"{API}/public/links/{token}")
    assert frame.status_code == 200 and frame.json()["purpose"] == "extension_confirmation"
    first = await anon.post(
        f"{API}/public/links/{token}/extension", json={"decision": "decline", "comment": "No"}
    )
    assert first.status_code == 200 and first.json()["status"] == "declined", first.text
    second = await anon.post(f"{API}/public/links/{token}/extension", json={"decision": "confirm"})
    assert second.status_code == 410


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await _propose(dorg, p["id"])).json()
    o = other_dorg.admin.client
    assert (await o.get(f"{API}/extensions/{ext['id']}")).status_code == 404
    assert (await o.get(f"{PL}/{p['id']}/extensions")).status_code == 404
    assert (await post(other_dorg, f"/extensions/{ext['id']}/confirm", {})).status_code == 404
    assert (
        await post(other_dorg, f"/placements/{p['id']}/extensions", {"new_end_date": _new_end()})
    ).status_code == 404
