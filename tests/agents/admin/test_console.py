# ruff: noqa: E501
"""Epic 65 story 65.1: the AI governance console -- run explorer, timeline, tool-call search, violations, costs,
summary, cross-tenant 404 and the Copilot privacy rule (no Copilot text in any console response)."""

import uuid
from typing import Any

from sqlalchemy import select, update

from app.core.database import tenant_session
from app.models.ai import AIAction
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg
from tests.agents.test_executor import start

API = "/api/v1"


def _script(cid: str) -> list[FakeTurn]:
    return [
        FakeTurn("tool_calls", calls=(
            ("tasks.create", {"title": "Call back", "entity_type": "candidate", "entity_id": cid}),
            ("messaging.send", {"recipient_kind": "candidate", "recipient_id": cid, "channel": "email",
                                "subject": "Hi", "body": "Secret body text"}),
            ("pipeline__reject_application", {"application_id": str(uuid.uuid4())}),
        )),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]  # fmt: skip


async def _run(org: AgentOrg, **kw: Any) -> uuid.UUID:
    with fake_llm(_script(str(org.candidate_id))):
        ref = await start(org, **kw)
    assert ref.run_id is not None
    return ref.run_id


async def test_console_reads(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    run_id = await _run(agent_org)
    c = agent_org.admin.client
    listed = (
        await c.get(
            f"{API}/ai-governance/runs", params={"agent_key": "_test_agent", "status": "completed,failed"}
        )
    ).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == str(run_id)
    assert (await c.get(f"{API}/ai-governance/runs", params={"tainted": True})).json()["total"] == 0
    detail = await c.get(f"{API}/ai-governance/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["copilot_private"] is False and d["cost_usd"]
    kinds = [i["kind"] for i in d["timeline"]]
    assert kinds[0] == "status" and "model_call" in kinds and "tool_call" in kinds and "proposal" in kinds
    ats = [i["at"] for i in d["timeline"]]
    assert ats == sorted(ats)
    assert d["steps"] and "model_tier" in d["steps"][0]

    calls = (await c.get(f"{API}/ai-governance/tool-calls", params={"run_id": str(run_id)})).json()
    assert calls["total"] == 3
    assert all("args_redacted" not in row and "result_summary" not in row for row in calls["items"])
    refused = (await c.get(f"{API}/ai-governance/violations", params={"kind": "refused_tool"})).json()
    assert any(r["run_id"] == str(run_id) for r in refused["items"])
    for kind in ("injection_detected", "possible_cross_scope"):
        assert (await c.get(f"{API}/ai-governance/violations", params={"kind": kind})).status_code == 200

    costs = (await c.get(f"{API}/ai-governance/costs", params={"group_by": "agent"})).json()
    async with tenant_session(agent_org.tenant_id) as db:
        from sqlalchemy import func

        total = (
            await db.execute(
                select(func.coalesce(func.sum(AIAction.cost_micros), 0)).where(AIAction.kind == "agent_step")
            )
        ).scalar_one()
    assert costs["totals"]["cost_micros"] == int(total)
    for g in ("day", "month"):
        assert (await c.get(f"{API}/ai-governance/costs", params={"group_by": g})).status_code == 200
    summary = (await c.get(f"{API}/ai-governance/summary")).json()
    assert summary["runs_today"].get("completed", 0) >= 1 and summary["pending_proposals"] == 1
    assert summary["violations_7d"] >= 1

    log = (
        await c.get(f"{API}/ai/actions", params={"kind": "agent_step", "agent_run_id": str(run_id)})
    ).json()
    assert log["total"] >= 1 and log["items"][0]["run_link"] == f"/admin/ai/runs/{run_id}"
    csv = await c.get(f"{API}/ai/actions/export.csv", params={"agent_run_id": str(run_id)})
    assert "agent_run_id" in csv.text.splitlines()[0]

    oc = other_org.admin.client
    for path in (f"/ai-governance/runs/{run_id}",):
        assert (await oc.get(f"{API}{path}")).status_code == 404
    assert (await oc.get(f"{API}/ai-governance/tool-calls", params={"run_id": str(run_id)})).json()[
        "total"
    ] == 0


async def test_copilot_runs_are_metadata_only(app: Any, agent_org: AgentOrg) -> None:
    run_id = await _run(agent_org, origin="copilot")
    c, h = agent_org.admin.client, agent_org.admin.h()
    d = (await c.get(f"{API}/ai-governance/runs/{run_id}")).json()
    assert d["copilot_private"] is True and d["payload_available"] is False and d["result"] is None
    assert all(tc["args_redacted"] is None and tc["result_summary"] is None for tc in d["tool_calls"])
    assert "Secret body text" not in str(d)
    payload = await c.post(
        f"{API}/agent-runs/{run_id}/payload", json={"reason": "Audit review of the run"}, headers=h
    )
    assert payload.status_code == 403 and payload.json()["error"]["code"] == "copilot_private"
    # A Copilot agent step in the P1 action log: content hidden from anyone but the initiator.
    async with tenant_session(agent_org.tenant_id) as db:
        await db.execute(
            update(AIAction).where(AIAction.agent_run_id == run_id)
            .values(agent_key="copilot", initiator_id=uuid.uuid4(), output={"text": "Copilot answer text"})
        )  # fmt: skip
        action_id = (
            (await db.execute(select(AIAction.id).where(AIAction.agent_run_id == run_id))).scalars().first()
        )
    one = (await c.get(f"{API}/ai/actions/{action_id}")).json()
    assert one["output"] is None and "Copilot answer text" not in str(one)
    listed = (await c.get(f"{API}/ai/actions", params={"agent_run_id": str(run_id)})).json()
    assert "Copilot answer text" not in str(listed)
    for path in (
        "/ai-governance/summary",
        "/ai-governance/costs",
        f"/ai-governance/tool-calls?run_id={run_id}",
    ):
        assert "Copilot answer text" not in (await c.get(f"{API}{path}")).text
