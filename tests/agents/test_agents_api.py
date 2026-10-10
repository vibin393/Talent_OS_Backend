# ruff: noqa: E501
"""Epic 53 API (spec 0.16): enablement, agent configuration codes, tool catalogue, runs, proposals, cross-tenant 404,
external principals."""

import uuid

from fastapi import FastAPI

from app.core.settings import get_settings
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg, enable
from tests.agents.test_executor import placeholder_key, start
from tests.modules.identity.conftest import join

API = "/api/v1"


async def test_enablement_and_agent_codes(agent_org: AgentOrg) -> None:
    c, h = agent_org.admin.client, agent_org.admin.h()
    await enable(agent_org.tenant_id, agentic=False)
    resp = await c.put(f"{API}/agents/{placeholder_key()}/enabled", json={"enabled": True}, headers=h)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "agentic_not_enabled"
    bad = await c.post(f"{API}/ai/agentic/enable", json={"notice_version": "1999-01-01"}, headers=h)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "notice_version_mismatch"
    ok = await c.post(
        f"{API}/ai/agentic/enable",
        json={"notice_version": get_settings().agent_enablement_notice_version},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["enabled"] is True
    resp = await c.put(f"{API}/agents/{placeholder_key()}/enabled", json={"enabled": True}, headers=h)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "agent_not_available"
    resp = await c.put(f"{API}/agents/_test_agent/actions/messaging.send", json={"mode": "auto"}, headers=h)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "autonomy_not_allowed"
    resp = await c.put(
        f"{API}/agents/_test_agent/actions/offers.release", json={"mode": "suggest"}, headers=h
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "tool_not_allowlisted"
    resp = await c.patch(f"{API}/agents/_test_agent", json={"limits": {"max_steps": 99}}, headers=h)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "limit_above_ceiling"
    resp = await c.put(
        f"{API}/agents/_test_agent/actions/tasks.create",
        json={"mode": "suggest", "route_role_key": "recruiter"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    tools = {t["key"]: t for t in resp.json()["tools"]}
    assert (
        tools["tasks.create"]["mode"] == "suggest" and tools["tasks.create"]["route_role_key"] == "recruiter"
    )
    listed = await c.get(f"{API}/agents")
    assert listed.status_code == 200 and {a["key"] for a in listed.json()} >= {
        "copilot",
        "demand",
        "analytics",
    }
    catalogue = (await c.get(f"{API}/agents/tools")).json()
    keys = {t["key"] for t in catalogue}
    assert "messaging.send" in keys and "application.rejected" in keys
    assert next(t for t in catalogue if t["key"] == "application.rejected")["forbidden"] is True


async def test_kill_switch_endpoint(agent_org: AgentOrg) -> None:
    c, h = agent_org.admin.client, agent_org.admin.h()
    resp = await c.post(f"{API}/ai/kill-switch", json={"on": True, "reason": "drill"}, headers=h)
    assert resp.status_code == 200 and resp.json()["kill_switch"] is True
    resp = await c.post(f"{API}/ai/kill-switch", json={"on": False}, headers=h)
    assert resp.json()["kill_switch"] is False


async def test_runs_and_proposals_api_cross_tenant(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    cid = str(agent_org.candidate_id)
    script = [
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "messaging.send",
                    {
                        "recipient_kind": "candidate",
                        "recipient_id": cid,
                        "channel": "email",
                        "subject": "Hi",
                        "body": "Hello",
                    },
                ),
            ),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]
    with fake_llm(script):
        ref = await start(agent_org)
    c, h = agent_org.admin.client, agent_org.admin.h()
    run = await c.get(f"{API}/agent-runs/{ref.run_id}")
    assert run.status_code == 200 and run.json()["status"] == "completed"
    assert run.json()["actor_label"].startswith("Test Agent for ")
    assert run.json()["candidate_ids"] == [cid]
    listed = await c.get(f"{API}/proposals", params={"status": "pending"})
    assert listed.status_code == 200 and listed.json()["total"] == 1
    pid = listed.json()["items"][0]["id"]
    assert listed.json()["items"][0]["can_decide"] is True
    # cross-tenant: 404
    oc = other_org.admin.client
    assert (await oc.get(f"{API}/agent-runs/{ref.run_id}")).status_code == 404
    assert (await oc.get(f"{API}/proposals/{pid}")).status_code == 404
    resp = await oc.post(
        f"{API}/proposals/{pid}/decide",
        json={"decision": "reject", "note": "nope"},
        headers={**other_org.admin.h(), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
    # decide through the API with an Idempotency-Key (replay returns the same body)
    key = {**h, "Idempotency-Key": str(uuid.uuid4())}
    first = await c.post(
        f"{API}/proposals/{pid}/decide", json={"decision": "reject", "note": "not now"}, headers=key
    )
    second = await c.post(
        f"{API}/proposals/{pid}/decide", json={"decision": "reject", "note": "not now"}, headers=key
    )
    assert first.status_code == 200 and first.json()["status"] == "rejected"
    assert second.json() == first.json()


async def test_manual_start_and_payload_permission(app: FastAPI, agent_org: AgentOrg) -> None:
    c, h = agent_org.admin.client, agent_org.admin.h()
    with fake_llm([FakeTurn("emit_result", result={"outcome": "nothing_to_do", "summary": "none"})]):
        resp = await c.post(
            f"{API}/agent-runs",
            json={
                "agent_key": "_test_agent",
                "subject_type": "candidate",
                "subject_id": str(agent_org.candidate_id),
            },
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
        )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    no_reason = await c.post(f"{API}/agent-runs/{run_id}/payload", json={"reason": "x"}, headers=h)
    assert no_reason.status_code == 422
    ok = await c.post(
        f"{API}/agent-runs/{run_id}/payload", json={"reason": "Audit review of the run"}, headers=h
    )
    assert ok.status_code == 200 and ok.json()["items"]
    recruiter = await join(app, agent_org.tenant, "recruiter", name="Rita Recruiter")
    try:
        denied = await recruiter.client.post(
            f"{API}/agent-runs/{run_id}/payload",
            json={"reason": "Audit review of the run"},
            headers=recruiter.h(),
        )
        assert denied.status_code == 403
        assert (await recruiter.client.get(f"{API}/agents")).status_code == 403
    finally:
        await recruiter.client.aclose()
