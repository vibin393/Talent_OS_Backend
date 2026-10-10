# ruff: noqa: E501
"""Epic 65 story 65.8 (FR-329): header search group ``agent_proposal`` (visible set, PRP- exact pin, cross-tenant)
and the ``agent_runs`` / ``agent_proposals`` list kinds (no args / preview / rationale / notes)."""

from fastapi import FastAPI

from tests.agents.admin.test_review_signals import _proposal
from tests.agents.conftest import AgentOrg
from tests.modules.identity.conftest import join

API = "/api/v1"


async def test_proposal_search(app: FastAPI, agent_org: AgentOrg, other_org: AgentOrg) -> None:
    pid = await _proposal(agent_org)
    c = agent_org.admin.client
    one = (await c.get(f"{API}/proposals/{pid}")).json()
    hid = one["human_id"]
    res = (await c.get(f"{API}/search/global", params={"q": hid})).json()
    group = next(g for g in res["groups"] if g["type"] == "agent_proposal")
    assert group["items"][0]["human_id"] == hid and group["items"][0]["exact"] is True
    assert res["exact"] is not None and res["exact"]["type"] == "agent_proposal"
    assert "Hello" not in str(res)  # titles only, never the message body

    recruiter = await join(app, agent_org.tenant, "recruiter", name="Rita Recruiter")
    try:
        r = (await recruiter.client.get(f"{API}/search/global", params={"q": hid})).json()
        hits = [i for g in r["groups"] if g["type"] == "agent_proposal" for i in g["items"]]
        assert hits == []  # not assigned / owned / queued
    finally:
        await recruiter.client.aclose()
    other = (await other_org.admin.client.get(f"{API}/search/global", params={"q": hid})).json()
    assert [i for g in other["groups"] if g["type"] == "agent_proposal" for i in g["items"]] == []


async def test_lists_are_masked(agent_org: AgentOrg) -> None:
    await _proposal(agent_org)
    c = agent_org.admin.client
    props = await c.get(f"{API}/reports/lists/agent_proposals")
    assert props.status_code == 200, props.text
    body = props.json()
    assert body["total"] >= 1
    keys = {col["key"] for col in body["columns"]}
    assert not keys & {"args", "preview", "rationale", "evidence", "result", "decision_note"}
    assert "Hello" not in props.text
    runs = await c.get(f"{API}/reports/lists/agent_runs")
    assert runs.status_code == 200 and runs.json()["total"] >= 1
    assert not {col["key"] for col in runs.json()["columns"]} & {"result", "args", "summary"}
