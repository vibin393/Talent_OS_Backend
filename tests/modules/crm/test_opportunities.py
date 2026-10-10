# ruff: noqa: E501
"""Epic 15 story 2.3 (A5): opportunities, the §5.2 stage machine, probability override / reset, weighted revenue,
the board (per-currency totals), list filters, summary and the Client 360 card."""

from decimal import Decimal

import pytest

from app.services.crm import opportunities as opp_svc
from app.services.crm.common import weighted
from tests.modules.crm.conftest import API, CrmOrg, JoinAs, create_client, create_opp, err, h, move, ok

ACTORS = ("user", "system", "ai", "workflow")
STAGES = ("qualification", "proposal", "negotiation", "won", "lost")
EXPECTED = {
    ("qualification", "proposal"),
    ("proposal", "negotiation"),
    ("qualification", "won"),
    ("proposal", "won"),
    ("negotiation", "won"),
    ("qualification", "lost"),
    ("proposal", "lost"),
    ("negotiation", "lost"),
    ("lost", "qualification"),
}


@pytest.mark.parametrize("frm", STAGES)
@pytest.mark.parametrize("to", STAGES)
@pytest.mark.parametrize("actor", ACTORS)
def test_stage_matrix(frm: str, to: str, actor: str) -> None:
    assert opp_svc.can_transition(frm, to, actor) == (actor == "user" and (frm, to) in EXPECTED)


@pytest.mark.parametrize(
    ("amount", "pct", "currency", "expected"),
    [
        ("100000.00", "40", "INR", "40000.00"),
        ("0.05", "50", "INR", "0.03"),  # 0.025 -> half-up
        ("333.33", "33.33", "USD", "111.10"),
        ("1001", "50", "JPY", "501"),  # 0 minor units, 500.5 -> 501
    ],
)
def test_weighted_rounding(amount: str, pct: str, currency: str, expected: str) -> None:
    assert format(weighted(Decimal(amount), Decimal(pct), currency), "f") == expected


async def test_pipeline_flow_probability_and_rules(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    client = await create_client(corg.admin, "Pipeline Co")
    opp = await create_opp(corg.admin, client["id"], lines=[{"title": "QA", "positions": 2}])
    assert opp["human_id"].startswith("OPP-") and opp["stage"] == "qualification"
    assert (opp["probability_pct"], opp["weighted_revenue"]) == ("10.00", "10000.00")
    assert opp["account_manager_id"] == str(corg.admin.user_id)
    # stale expected_stage
    assert err(await move(corg.admin, opp, "negotiation", expected_stage="proposal")) == (409, "stage_conflict")
    # skipping / backward moves refused
    assert err(await move(corg.admin, opp, "negotiation")) == (409, "transition_not_allowed")
    # won only through the conversion
    assert err(await move(corg.admin, opp, "won")) == (409, "use_won_conversion")
    p = await ok(await move(corg.admin, opp, "proposal"))
    assert (p["stage"], p["probability_pct"]) == ("proposal", "40.00")
    assert err(await move(corg.admin, p, "qualification")) == (409, "transition_not_allowed")
    # override kept across stage changes; reset restores the default
    o = await ok(await c.patch(f"{API}/opportunities/{opp['id']}", json={"probability_pct": "55"}, headers=hh))
    assert (o["probability_pct"], o["probability_overridden"], o["weighted_revenue"]) == ("55.00", True, "55000.00")
    n = await ok(await move(corg.admin, o, "negotiation"))
    assert (n["stage"], n["probability_pct"]) == ("negotiation", "55.00")
    r = await ok(await c.post(f"{API}/opportunities/{opp['id']}/reset-probability", headers=hh))
    assert (r["probability_pct"], r["probability_overridden"]) == ("70.00", False)
    # lost needs a reason
    assert err(await move(corg.admin, r, "lost")) == (422, "reason_code_required")
    lost = await ok(await move(corg.admin, r, "lost", reason_code="competitor", competitor="BigCo"))
    assert (lost["stage"], lost["lost_competitor"], lost["probability_pct"]) == ("lost", "BigCo", "0.00")
    # closed: commercial fields frozen, owner / AM allowed
    assert err(await c.patch(f"{API}/opportunities/{opp['id']}", json={"name": "X"}, headers=hh)) == (409, "opportunity_closed")
    ok_owner = await c.patch(f"{API}/opportunities/{opp['id']}", json={"account_manager_id": str(corg.admin.user_id)}, headers=hh)
    assert ok_owner.status_code == 200
    # reopen needs a reopen reason
    assert err(await move(corg.admin, lost, "qualification")) == (422, "reason_code_required")
    reopened = await ok(await move(corg.admin, lost, "qualification", reason_code="need_revived"))
    assert (reopened["stage"], reopened["lost_reason_code"], reopened["probability_pct"]) == ("qualification", None, "10.00")
    hist = await ok(await c.get(f"{API}/opportunities/{opp['id']}/history"))
    assert [x["to_value"] for x in hist] == ["qualification", "proposal", "negotiation", "lost", "qualification"]
    # stage changes are on the opportunity and client timelines
    tl = await ok(await c.get(f"{API}/clients/{client['id']}/timeline", params={"limit": 50}))
    assert any("Stage changed" in (i["body"] or "") for i in tl["items"])
    # lines replace-set
    lines = await ok(await c.put(f"{API}/opportunities/{opp['id']}/lines", json={"lines": [{"title": "Dev", "positions": 3}, {"title": "PM"}]}, headers=hh))
    assert [(x["title"], x["positions"]) for x in lines["lines"]] == [("Dev", 3), ("PM", 1)]


async def test_direct_create_needs_open_client(corg: CrmOrg) -> None:
    inactive = await create_client(corg.admin, "Old Co", status="inactive")
    resp = await corg.admin.client.post(
        f"{API}/opportunities", json={"name": "N", "client_id": inactive["id"]}, headers=h(corg.admin)
    )
    assert err(resp) == (409, "client_not_open")


async def test_board_list_summary_and_client_card(corg: CrmOrg) -> None:
    c = corg.admin.client
    client = await create_client(corg.admin, "Board Co")
    a = await create_opp(corg.admin, client["id"], "A", estimated_revenue="1000.00", expected_close_date="2026-10-20")
    await create_opp(corg.admin, client["id"], "B", estimated_revenue="2000.00", currency="USD", expected_close_date="2026-11-05")
    await create_opp(corg.admin, client["id"], "C", estimated_revenue="333.33", currency="USD")
    await ok(await move(corg.admin, a, "proposal"))
    board = await ok(await c.get(f"{API}/opportunities/board"))
    cols = {col["stage"]: col for col in board["columns"]}
    assert list(cols) == ["qualification", "proposal", "negotiation", "won", "lost"]
    assert cols["qualification"]["count"] == 2
    assert cols["qualification"]["totals"] == [{"currency": "USD", "estimated": "2333.33", "weighted": "233.33"}]
    assert cols["proposal"]["totals"] == [{"currency": "INR", "estimated": "1000.00", "weighted": "400.00"}]
    page = await ok(await c.get(f"{API}/opportunities", params=[("stage", "qualification"), ("stage", "proposal"), ("close_from", "2026-10-01"), ("close_to", "2026-10-31")]))
    assert [x["name"] for x in page["items"]] == ["A"] and page["items"][0]["weighted_revenue"] == "400.00"
    summ = await ok(await c.get(f"{API}/opportunities/summary", params={"group_by": "close_month"}))
    keys = {g["key"]: g for g in summ["groups"]}
    assert set(keys) == {"2026-10", "2026-11", "none"}
    summ = await ok(await c.get(f"{API}/opportunities/summary", params={"group_by": "owner"}))
    assert summ["groups"][0]["label"] == "Sam Seller" and summ["groups"][0]["count"] == 3
    card = await ok(await c.get(f"{API}/clients/{client['id']}/opportunities"))
    assert card["open_count"] == 3
    assert {t["currency"] for t in card["weighted_pipeline"]} == {"INR", "USD"}


async def test_opportunity_scope(corg: CrmOrg, join_as: JoinAs) -> None:
    rep = await join_as("sales", name="Rita Rep")
    client = await create_client(rep, "Rep Client")
    mine = await create_opp(rep, client["id"], "Rep deal")
    theirs = await create_opp(corg.admin, client["id"], "Admin deal")
    page = await ok(await rep.client.get(f"{API}/opportunities"))
    assert [x["id"] for x in page["items"]] == [mine["id"]]
    assert (await rep.client.get(f"{API}/opportunities/{theirs['id']}")).status_code == 404
    board = await ok(await rep.client.get(f"{API}/opportunities/board"))
    assert sum(col["count"] for col in board["columns"]) == 1
