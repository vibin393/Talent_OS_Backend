# ruff: noqa: E501
"""Epic 15 story 2.4 (A6): Won preview (no writes), the one-step Won conversion (full / partial selections),
rollback on a failing step, Idempotency-Key replay, back-links and the Account Manager hand-off."""

import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.commercials import Contract, RateCard
from app.models.crm import CrmRecordLink
from app.models.demands import Demand
from app.models.notifications import Notification
from tests.modules.crm.conftest import API, CrmOrg, JoinAs, create_client, create_opp, err, h, move, ok, stored_pdf

LINES = [
    {"title": "Java Developer", "positions": 2, "expected_bill_rate": "1500.1250", "rate_unit": "hour", "location": "Pune"},
    {"title": "QA Engineer", "positions": 1, "expected_bill_rate": "900.0000", "rate_unit": "hour"},
]


async def _negotiation(org: CrmOrg, am_id: uuid.UUID | None = None, **extra: Any) -> dict[str, Any]:
    client = await create_client(org.admin, f"Won Client {uuid.uuid4().hex[:6]}")
    body: dict[str, Any] = {"lines": LINES, **extra}
    if am_id is not None:
        body["account_manager_id"] = str(am_id)
    opp = await create_opp(org.admin, client["id"], "Big deal", **body)
    opp = await ok(await move(org.admin, opp, "proposal"))
    return await ok(await move(org.admin, opp, "negotiation"))


async def _counts(org: CrmOrg) -> tuple[int, int, int, int]:
    async with tenant_session(org.tenant_id) as db:
        return (
            int((await db.execute(select(func.count()).select_from(Contract))).scalar_one()),
            int((await db.execute(select(func.count()).select_from(RateCard).where(RateCard.is_template.is_(False)))).scalar_one()),
            int((await db.execute(select(func.count()).select_from(Demand))).scalar_one()),
            int((await db.execute(select(func.count()).select_from(CrmRecordLink))).scalar_one()),
        )


async def _template(org: CrmOrg) -> str:
    tpl = await ok(await org.admin.client.post(f"{API}/rate-card-templates", json={"name": "Std IT"}, headers=h(org.admin)), 201)
    return str(tpl["id"])


async def test_preview_has_no_writes(corg: CrmOrg) -> None:
    opp = await _negotiation(corg)
    before = await _counts(corg)
    prev = await ok(await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/won-preview", headers=h(corg.admin)))
    assert prev["client"]["will_activate"] is True and prev["client"]["current_status"] == "prospect"
    assert prev["contract"]["type"] == "msa"
    assert prev["contract"]["legal_entity_id"] == str(corg.entity_id)
    assert [d["title"] for d in prev["demands"]] == ["Java Developer", "QA Engineer"]
    assert prev["demands"][0]["owner_id"] == str(corg.admin.user_id)
    assert prev["commercials_available"] is True
    assert await _counts(corg) == before
    got = await ok(await corg.admin.client.get(f"{API}/opportunities/{opp['id']}"))
    assert got["stage"] == "negotiation"


async def test_full_win_creates_everything_and_replays(corg: CrmOrg, join_as: JoinAs) -> None:
    am = await join_as("tenant_admin", name="Amy Manager")
    opp = await _negotiation(corg, am.user_id)
    file_id = await stored_pdf(corg.tenant_id)
    template_id = await _template(corg)
    body = {
        "activate_client": True,
        "contract": {"create": True, "type": "msa", "title": "Acme MSA", "file_ids": [str(file_id)]},
        "rate_card": {"template_id": template_id, "effective_from": "2026-10-05"},
        "demands": [{"line_id": ln["id"], "create": True} for ln in opp["lines"]],
    }
    key = {"Idempotency-Key": f"win-{uuid.uuid4()}"}
    resp = await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), **key})
    out = await ok(resp)
    assert out["opportunity"]["stage"] == "won" and out["opportunity"]["probability_pct"] == "100.00"
    assert out["client_activated"] is True
    assert len(out["demand_ids"]) == 2 and out["contract_id"] and out["rate_card_id"]
    client = await ok(await corg.admin.client.get(f"{API}/clients/{out['client_id']}"))
    assert client["status"] == "active"
    async with tenant_session(corg.tenant_id) as db:
        contract = await db.get(Contract, uuid.UUID(out["contract_id"]))
        assert contract is not None and (contract.status, contract.contract_type, contract.opportunity_id) == ("draft", "msa", uuid.UUID(opp["id"]))
        card = await db.get(RateCard, uuid.UUID(out["rate_card_id"]))
        assert card is not None and card.status == "draft" and card.contract_id == contract.id
        demands = list((await db.execute(select(Demand).where(Demand.id.in_([uuid.UUID(i) for i in out["demand_ids"]])))).scalars())
        assert {d.status for d in demands} == {"draft"}
        assert {d.owner_id for d in demands} == {am.user_id}
        java = next(d for d in demands if d.title == "Java Developer")
        assert (java.positions, str(java.bill_rate), java.location) == (2, "1500.13", "Pune")
        notes = list((await db.execute(select(Notification).where(Notification.type == "crm.opportunity_won_handoff"))).scalars())
        assert [n.user_id for n in notes] == [am.user_id]
    before = await _counts(corg)
    # replay: same response, nothing new
    again = await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), **key})
    assert again.status_code == 200 and again.headers.get("Idempotency-Replayed") == "true"
    assert again.json() == out
    assert await _counts(corg) == before
    # back-links
    for kind, rid in (("contract", out["contract_id"]), ("demand", out["demand_ids"][0]), ("client", out["client_id"])):
        link = await ok(await corg.admin.client.get(f"{API}/crm/links", params={"record_kind": kind, "record_id": rid}))
        assert link == {"opportunity_id": opp["id"], "human_id": opp["human_id"], "name": "Big deal"}
    none = await ok(await corg.admin.client.get(f"{API}/crm/links", params={"record_kind": "demand", "record_id": str(uuid.uuid4())}))
    assert none is None
    # won: commercial fields frozen, lines frozen, no second win
    assert err(await corg.admin.client.patch(f"{API}/opportunities/{opp['id']}", json={"estimated_revenue": "1"}, headers=h(corg.admin))) == (409, "opportunity_closed")
    assert err(await corg.admin.client.put(f"{API}/opportunities/{opp['id']}/lines", json={"lines": []}, headers=h(corg.admin))) == (409, "opportunity_closed")
    other = await corg.admin.client.post(
        f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), "Idempotency-Key": "other-key"}
    )
    assert err(other) == (409, "transition_not_allowed")


async def test_win_with_everything_deselected(corg: CrmOrg) -> None:
    opp = await _negotiation(corg)
    before = await _counts(corg)
    body = {"activate_client": True, "contract": {"create": False}, "rate_card": None, "demands": [{"line_id": ln["id"], "create": False} for ln in opp["lines"]]}
    out = await ok(await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), "Idempotency-Key": "k1"}))
    assert out["opportunity"]["stage"] == "won" and out["client_activated"] is True
    assert (out["contract_id"], out["rate_card_id"], out["demand_ids"]) == (None, None, [])
    after = await _counts(corg)
    assert after[:3] == before[:3] and after[3] == before[3] + 1  # only the client link


async def test_win_rolls_back_on_failing_demand(corg: CrmOrg) -> None:
    opp = await _negotiation(corg)
    before = await _counts(corg)
    body = {
        "contract": {"create": True, "type": "msa"},
        "demands": [
            {"line_id": opp["lines"][0]["id"], "create": True},
            {"line_id": opp["lines"][1]["id"], "create": True, "overrides": {"title": "x" * 301}},
        ],
    }
    resp = await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), "Idempotency-Key": "k2"})
    assert resp.status_code == 422, resp.text
    # schema-level failure on the overrides; now a service-level one (positions below 1 cannot be sent, so use a
    # hiring manager contact that no longer exists: archive is not enough -- use an unknown template instead)
    body = {
        "contract": {"create": True, "type": "msa"},
        "rate_card": {"template_id": str(uuid.uuid4()), "effective_from": "2026-10-05"},
        "demands": [{"line_id": opp["lines"][0]["id"], "create": True}],
    }
    resp = await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(corg.admin), "Idempotency-Key": "k3"})
    assert resp.status_code in (404, 422), resp.text
    assert resp.json()["error"]["details"]["step"] == "rate_card"
    assert await _counts(corg) == before
    got = await ok(await corg.admin.client.get(f"{API}/opportunities/{opp['id']}"))
    assert got["stage"] == "negotiation"
    client = await ok(await corg.admin.client.get(f"{API}/clients/{got['client_id']}"))
    assert client["status"] == "prospect"


async def test_demand_step_failure_carries_line_id(corg: CrmOrg, join_as: JoinAs) -> None:
    # a Sales user without demand:create -> the demand step fails, everything rolls back
    rep = await join_as("sales", name="Rita Rep")
    client = await create_client(rep, "Rep Won Co")
    opp = await create_opp(rep, client["id"], "Rep deal", lines=LINES)
    body = {"demands": [{"line_id": opp["lines"][0]["id"], "create": True}]}
    resp = await rep.client.post(f"{API}/opportunities/{opp['id']}/win", json=body, headers={**h(rep), "Idempotency-Key": "k4"})
    assert resp.status_code == 403, resp.text
    details = resp.json()["error"]["details"]
    assert (details["step"], details["line_id"]) == ("demand", opp["lines"][0]["id"])
    got = await ok(await rep.client.get(f"{API}/opportunities/{opp['id']}"))
    assert got["stage"] == "qualification"


async def test_win_requires_idempotency_key(corg: CrmOrg) -> None:
    opp = await _negotiation(corg)
    resp = await corg.admin.client.post(f"{API}/opportunities/{opp['id']}/win", json={}, headers=h(corg.admin))
    assert err(resp) == (422, "idempotency_key_required")
