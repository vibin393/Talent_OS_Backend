# ruff: noqa: E501
"""Epic 65 story 65.3: ending the burn-in early (reason >= 10 chars, audited, 409 when inactive, API key 403), the
checklist acknowledgement, the enablement checklist and the per-agent limits."""

from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from tests.agents.conftest import AgentOrg
from tests.modules.identity.conftest import new_client

API = "/api/v1"


async def _audit_actions(org: AgentOrg) -> list[tuple[str, Any]]:
    async with tenant_session(org.tenant_id) as db:
        return [
            (r.action, r.data)
            for r in (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        ]


async def test_burn_in_end(app: FastAPI, agent_org: AgentOrg) -> None:
    c, h = agent_org.admin.client, agent_org.admin.h()
    resp = await c.put(f"{API}/agents/_test_agent/enabled", json={"enabled": True}, headers=h)
    assert resp.status_code == 200 and resp.json()["burn_in_until"] is not None
    short = await c.post(f"{API}/agents/_test_agent/burn-in/end", json={"reason": "ok"}, headers=h)
    assert short.status_code == 422 and short.json()["error"]["code"] == "reason_required"
    ok = await c.post(
        f"{API}/agents/_test_agent/burn-in/end",
        json={"reason": "Pilot finished, quality reviewed"},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    again = await c.post(
        f"{API}/agents/_test_agent/burn-in/end",
        json={"reason": "Pilot finished, quality reviewed"},
        headers=h,
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "burn_in_not_active"
    ack = await c.post(
        f"{API}/agents/_test_agent/checklist/acknowledge",
        json={"reason": "Calendars connect next week"},
        headers=h,
    )
    assert ack.status_code == 204
    actions = await _audit_actions(agent_org)
    ended = [d for a, d in actions if a == "agent.burn_in_ended"]
    assert ended and ended[0]["reason"] == "Pilot finished, quality reviewed"
    assert any(a == "agent.checklist_overridden" for a, _ in actions)

    # Epic 66: ``ai:agent_configure`` is NON_DELEGABLE -- an API key can never hold it.
    refused = await c.post(
        f"{API}/api-keys", json={"name": "ops", "scopes": ["ai:agent_configure", "ai:govern"]}, headers=h
    )
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "scope_not_delegable"
    created = await c.post(f"{API}/api-keys", json={"name": "ops", "scopes": ["ai:govern"]}, headers=h)
    assert created.status_code == 201, created.text
    async with new_client(app) as client:
        auth = {"Authorization": f"Bearer {created.json()['key']}"}
        denied = await client.post(
            f"{API}/agents/_test_agent/burn-in/end",
            json={"reason": "Pilot finished, quality reviewed"},
            headers=auth,
        )
        assert denied.status_code == 403 and denied.json()["error"]["code"] in ("human_only_action", "forbidden")
        assert (
            await client.post(
                f"{API}/agents/_test_agent/checklist/acknowledge", json={"reason": "x" * 12}, headers=auth
            )
        ).status_code == 403
        assert (
            await client.get(f"{API}/ai-governance/summary", headers=auth)
        ).status_code == 200  # GETs allowed (FR-329)


async def test_checklist_and_limits(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    c = agent_org.admin.client
    resp = await c.get(f"{API}/agents/screening/checklist")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = {i["key"]: i for i in body["items"]}
    assert keys["master_switch"]["status"] == "ready" and keys["master_switch"]["blocking"] is True
    assert {"questionnaires", "consent_notice", "aedt_entry", "eval_passing"} <= set(keys)
    assert keys["questionnaires"]["status"] in ("missing", "warning") and body["ready"] is False
    assert keys["eval_passing"]["status"] in ("warning", "ready")  # ready once a live result exists (store test)
    assert (await c.get(f"{API}/agents/nope/checklist")).status_code == 404
    limits = (await c.get(f"{API}/agents/screening/limits")).json()
    assert limits["ceiling"]["max_steps"] >= 1
    sched = (await c.get(f"{API}/agents/scheduling/checklist")).json()
    assert any(i["key"] == "calendars" for i in sched["items"])
    assert (await other_org.admin.client.get(f"{API}/agents/screening/checklist")).status_code == 200
