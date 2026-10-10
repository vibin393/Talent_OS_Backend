# ruff: noqa: E501
"""Epic 65 story 65.4 (FR-321): the "AI agents" and "Agent outcomes" dashboards -- every tile computes (ok or
unavailable, never an error), gating, the agent filter, no content in tiles, and list parity."""

from datetime import date

from fastapi import FastAPI

from app.services.reporting.dashboards_p4 import P4_DASHBOARDS
from tests.agents.admin.test_console import _run
from tests.agents.conftest import AgentOrg
from tests.modules.identity.conftest import join

API = "/api/v1"


async def test_tiles_compute_and_parity(agent_org: AgentOrg) -> None:
    run_id = await _run(agent_org)
    c = agent_org.admin.client
    listed = (await c.get(f"{API}/dashboards")).json()["items"]
    keys = [d["key"] for d in listed]
    assert "agents" in keys and "agent_outcomes" in keys
    today = date.today().isoformat()
    for dash in ("agents", "agent_outcomes"):
        for tile in P4_DASHBOARDS[dash].tiles:
            resp = await c.get(
                f"{API}/dashboards/{dash}/tiles/{tile.key}", params={"from": today, "to": today}
            )
            assert resp.status_code == 200, (dash, tile.key, resp.text)
            body = resp.json()
            assert body["status"] in ("ok", "unavailable"), (tile.key, body)
            assert "Secret body text" not in resp.text
    by_status = (
        await c.get(
            f"{API}/dashboards/agents/tiles/agent_runs_by_status",
            params={"from": today, "to": today, "agent_key": "_test_agent"},
        )
    ).json()
    runs_total = (await c.get(f"{API}/agent-runs", params={"agent_key": "_test_agent"})).json()["total"]
    assert by_status["value"] == runs_total
    outcomes = (
        await c.get(f"{API}/dashboards/agents/tiles/proposal_outcomes", params={"from": today, "to": today})
    ).json()
    props_total = (await c.get(f"{API}/proposals")).json()["total"]
    assert outcomes["value"] == props_total
    cost_tile = (
        await c.get(f"{API}/dashboards/agents/tiles/agent_tokens_cost", params={"from": today, "to": today})
    ).json()
    costs = (await c.get(f"{API}/ai-governance/costs", params={"from": today, "to": today})).json()
    micros = next(r["value"] for r in cost_tile["rows"] if r["key"] == "cost_micros")
    assert micros == costs["totals"]["cost_micros"]
    other = (
        await c.get(
            f"{API}/dashboards/agents/tiles/agent_runs_by_status",
            params={"from": today, "to": today, "agent_key": "screening"},
        )
    ).json()
    assert other["value"] == 0
    sm30 = (await c.get(f"{API}/dashboards/agent_outcomes/tiles/sm30_coordination_time")).json()
    assert sm30["estimate"] is True and sm30["badge"] == "Estimate"
    c16 = (await c.get(f"{API}/dashboards/agent_outcomes/tiles/smc16_impact_ratios")).json()
    assert c16["value"] is None and c16["link"]["path"] == "/fairness" and not c16.get("rows")
    assert run_id


async def test_gating(app: FastAPI, agent_org: AgentOrg) -> None:
    recruiter = await join(app, agent_org.tenant, "recruiter", name="Rita Recruiter")
    try:
        keys = [d["key"] for d in (await recruiter.client.get(f"{API}/dashboards")).json()["items"]]
        assert "agents" not in keys
        assert (
            await recruiter.client.get(f"{API}/dashboards/agents/tiles/agent_scorecard")
        ).status_code == 404
    finally:
        await recruiter.client.aclose()
