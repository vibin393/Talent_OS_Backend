"""Workflow engine (FR-58/59): live runs, loop protection, partial failures, dry runs without side effects."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.services import workflow
from app.services.workflow import engine
from tests.epic10 import API, RULES, WORKFLOW, DOrg, count, create_demand, h, member, open_demand, rows


async def _team_with(m: Any, user_id: uuid.UUID) -> str:
    resp = await m.client.post(
        f"{API}/admin/teams", json={"name": f"Team A {uuid.uuid4().hex[:5]}"}, headers=h(m)
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]
    resp = await m.client.put(
        f"{API}/admin/teams/{team_id}/members/{user_id}", json={"is_lead": False}, headers=h(m)
    )
    assert resp.status_code == 200, resp.text
    return str(team_id)


def _acceptance_rule(recruiter_id: uuid.UUID, team_id: str) -> dict[str, Any]:
    return {
        "name": "Demand opened & priority=high",
        "trigger": "demand.status_changed",
        "conditions": {
            "all": [
                {"field": "event.to_status", "op": "eq", "value": "open"},
                {"field": "priority", "op": "eq", "value": "high"},
            ]
        },
        "actions": [
            {"type": "assign", "params": {"user_ids": [str(recruiter_id)], "team_ids": [team_id], "primary_user_id": str(recruiter_id)}},  # noqa: E501
            {"type": "create_task", "params": {"title": "Kick-off call for {{ demand.human_id }}", "owner": "primary_recruiter", "due_in_hours": 4}},  # noqa: E501
            {"type": "notify", "params": {"recipients": ["primary_recruiter"], "message": "New high-priority demand"}},  # noqa: E501
        ],
    }  # fmt: skip


async def _counts(tenant_id: uuid.UUID) -> dict[str, int]:
    tables = ("tasks", "notifications", "email_messages", "demand_assignments", "audit_logs", "activities")
    return {t: await count(tenant_id, t) for t in tables}


async def test_acceptance_open_high_priority_demand(app: FastAPI, dorg: DOrg) -> None:
    rec = await member(app, dorg, "recruiter", name="Priya Recruiter")
    team_id = await _team_with(dorg.admin, rec.user_id)
    resp = await dorg.admin.client.post(
        RULES, json=_acceptance_rule(rec.user_id, team_id), headers=h(dorg.admin)
    )
    assert resp.status_code == 201 and resp.json()["is_active"] is False
    rule_id = resp.json()["id"]

    # dry run first: three would-run actions, nothing sent
    draft = await open_demand(dorg.admin, dorg.client_id, priority="high")  # rule inactive: no run
    before = await _counts(dorg.tenant_id)
    dry = await dorg.admin.client.post(
        f"{RULES}/{rule_id}/dry-run",
        json={"entity_id": draft["id"], "extras": {"to_status": "open"}},
        headers=h(dorg.admin),
    )
    assert dry.status_code == 200, dry.text
    out = dry.json()
    assert out["matched"] is True and out["note"] == "Nothing was executed or sent."
    statuses = [a["status"] for a in out["action_results"]]
    assert statuses == ["would_run", "would_run", "would_run"], out["action_results"]
    assert "Would create task" in out["action_results"][1]["detail"]
    assert "Priya Recruiter" in out["action_results"][1]["detail"]  # the primary the assign would set
    assert await _counts(dorg.tenant_id) == before  # zero side effects (the dry-run row is not audited)
    assert await count(dorg.tenant_id, "workflow_executions", "mode = 'dry_run'") == 1

    assert (
        await dorg.admin.client.post(f"{RULES}/{rule_id}/activate", headers=h(dorg.admin))
    ).status_code == 200
    demand = await open_demand(dorg.admin, dorg.client_id, priority="high")
    (execution,) = await rows(
        dorg.tenant_id,
        "SELECT * FROM workflow_executions WHERE rule_id = :r AND mode = 'live'",
        r=uuid.UUID(rule_id),
    )
    assert execution["status"] == "succeeded", execution["action_results"]
    assert (execution["finished_at"] - execution["enqueued_at"]).total_seconds() < 60
    assignments = await dorg.admin.client.get(f"{API}/demands/{demand['id']}/assignments")
    items = assignments.json()["items"]
    assert any(a["user_id"] == str(rec.user_id) and a["is_primary"] for a in items)
    assert any(a["team_id"] == team_id for a in items)
    (task,) = await rows(
        dorg.tenant_id, "SELECT * FROM tasks WHERE entity_id = :d", d=uuid.UUID(demand["id"])
    )
    assert (
        task["owner_id"] == rec.user_id and task["source"] == "workflow" and demand["req_id"] in task["title"]
    )
    assert task["workflow_execution_id"] == execution["id"]
    assert (
        await count(
            dorg.tenant_id, "notifications", "type = 'workflow_notice' AND user_id = :u", u=rec.user_id
        )
        == 1
    )
    # audit shows the workflow rule as the actor of the assignment
    audit = await rows(
        dorg.tenant_id,
        "SELECT actor_type, actor_id FROM audit_logs WHERE entity_type = 'task' AND entity_id = :t",
        t=str(task["id"]),
    )
    assert audit and audit[0]["actor_type"] == "workflow" and audit[0]["actor_id"] == uuid.UUID(rule_id)
    # a low-priority demand does not match
    await open_demand(dorg.admin, dorg.client_id, priority="low")
    got = await rows(
        dorg.tenant_id,
        "SELECT status FROM workflow_executions WHERE rule_id = :r AND mode = 'live' ORDER BY created_at",
        r=uuid.UUID(rule_id),
    )
    assert [g["status"] for g in got] == ["succeeded", "not_matched"]
    listed = await dorg.admin.client.get(f"{RULES}/{rule_id}/executions", params={"mode": "live"})
    assert listed.json()["total"] == 2
    detail = await dorg.admin.client.get(f"{WORKFLOW}/executions/{execution['id']}")
    assert detail.status_code == 200 and detail.json()["condition_trace"]
    await rec.client.aclose()


async def test_non_eager_enqueues_without_countdown(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import workflow as workflow_tasks

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(workflow_tasks.evaluate_event, "delay", lambda *a, **k: calls.append((a, k)))
    await create_demand(dorg.admin, dorg.client_id)
    assert calls, "DemandCreated must enqueue workflow.evaluate_event"
    args, kwargs = calls[0]
    assert kwargs == {} and args[1] == "demand.created" and args[2] == "demand"


async def _rule(
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    trigger: str,
    actions: list[dict[str, Any]],
    conds: dict[str, Any] | None = None,
) -> uuid.UUID:
    from app.models.workflow import WorkflowRule

    rule = WorkflowRule(
        id=uuid.uuid4(), tenant_id=tenant_id, name=f"r-{uuid.uuid4().hex[:6]}", trigger=trigger,
        conditions=conds or {}, actions=actions, is_active=True, version=1, created_by=created_by,
    )  # fmt: skip
    async with tenant_session(tenant_id) as db:
        db.add(rule)
    return rule.id


async def _evaluate(
    tenant_id: uuid.UUID,
    trigger: str,
    entity_type: str,
    entity_id: uuid.UUID,
    depth: int = 0,
    extras: dict[str, Any] | None = None,
) -> list[uuid.UUID]:
    async with tenant_session(tenant_id) as db:
        return await workflow.evaluate_event(
            db, trigger, entity_type, entity_id, uuid.uuid4(), extras or {}, depth
        )


async def test_partial_failure_isolation(dorg: DOrg) -> None:
    demand = await create_demand(dorg.admin, dorg.client_id)
    rule_id = await _rule(
        dorg.tenant_id,
        dorg.admin.user_id,
        "demand.created",
        [
            {"type": "create_task", "params": {"title": "ok", "owner": "entity_owner"}},
            {"type": "send_email", "params": {"template_id": str(uuid.uuid4()), "to": "primary_contact"}},
            {"type": "create_task", "params": {"title": "still runs", "owner": "entity_owner"}},
        ],
    )
    await _evaluate(dorg.tenant_id, "demand.created", "demand", uuid.UUID(demand["id"]))
    (execution,) = await rows(
        dorg.tenant_id, "SELECT * FROM workflow_executions WHERE rule_id = :r", r=rule_id
    )
    assert execution["status"] == "partially_failed"
    assert [a["status"] for a in execution["action_results"]] == ["ok", "error", "ok"]
    assert execution["action_results"][1]["error_code"] == "template_not_found"
    assert await count(dorg.tenant_id, "tasks", "workflow_execution_id = :e", e=execution["id"]) == 2
    # admins hear about failures
    assert (
        await count(
            dorg.tenant_id, "notifications", "type = 'workflow_failed' AND user_id = :u", u=dorg.admin.user_id
        )
        == 1
    )


async def test_loop_protection(dorg: DOrg) -> None:
    demand_id = uuid.UUID((await create_demand(dorg.admin, dorg.client_id))["id"])
    rule_id = await _rule(
        dorg.tenant_id,
        dorg.admin.user_id,
        "demand.created",
        [{"type": "create_task", "params": {"title": "t", "owner": "entity_owner"}}],
    )
    await _evaluate(dorg.tenant_id, "demand.created", "demand", demand_id, depth=4)
    await _evaluate(dorg.tenant_id, "demand.created", "demand", demand_id, depth=0)
    await _evaluate(dorg.tenant_id, "demand.created", "demand", demand_id, depth=0)  # within 60 s
    got = await rows(
        dorg.tenant_id,
        "SELECT status FROM workflow_executions WHERE rule_id = :r ORDER BY created_at",
        r=rule_id,
    )
    assert [g["status"] for g in got] == ["skipped_loop", "succeeded", "skipped_rate_limit"]


async def test_chain_depth_from_workflow_context() -> None:
    from app.core.context import RequestContext, reset_context, set_context
    from app.events.demands import DemandCreated
    from app.services.workflow.registry import chain_depth_of

    event = DemandCreated(tenant_id=uuid.uuid4(), demand_id=uuid.uuid4(), actor_kind="user")
    assert chain_depth_of(event) == 0
    token = set_context(RequestContext(actor_kind="workflow", workflow_chain_depth=2))
    try:
        assert chain_depth_of(event) == 3
    finally:
        reset_context(token)


async def test_same_event_redelivered_runs_once(dorg: DOrg) -> None:
    demand_id = uuid.UUID((await create_demand(dorg.admin, dorg.client_id))["id"])
    rule_id = await _rule(dorg.tenant_id, dorg.admin.user_id, "demand.created", [])
    event_id = uuid.uuid4()
    for _ in range(2):
        async with tenant_session(dorg.tenant_id) as db:
            await workflow.evaluate_event(db, "demand.created", "demand", demand_id, event_id, {}, 0)
    assert await count(dorg.tenant_id, "workflow_executions", "rule_id = :r", r=rule_id) == 1


async def test_forged_date_comparison_is_false_with_note(dorg: DOrg) -> None:
    demand_id = uuid.UUID((await create_demand(dorg.admin, dorg.client_id))["id"])
    rule_id = await _rule(
        dorg.tenant_id, dorg.admin.user_id, "demand.created", [],
        {"all": [{"field": "positions", "op": "gt", "value": "abc"}]},
    )  # fmt: skip
    await _evaluate(dorg.tenant_id, "demand.created", "demand", demand_id)
    (execution,) = await rows(
        dorg.tenant_id, "SELECT * FROM workflow_executions WHERE rule_id = :r", r=rule_id
    )
    assert execution["status"] == "not_matched"
    assert "incompatible" in execution["condition_trace"][0]["note"]


async def test_dry_run_unsaved_rule_and_cross_tenant(dorg: DOrg, other_dorg: DOrg) -> None:
    demand = await open_demand(dorg.admin, dorg.client_id, priority="high")
    body = {
        "entity_id": demand["id"],
        "rule": {
            "name": "draft",
            "trigger": "demand.created",
            "conditions": {"all": [{"field": "priority", "op": "eq", "value": "low"}]},
            "actions": [{"type": "notify", "params": {"recipients": ["entity_owner"], "message": "hi"}}],
        },
    }
    resp = await dorg.admin.client.post(f"{WORKFLOW}/dry-run", json=body, headers=h(dorg.admin))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["matched"] is False and out["execution_id"] is None
    assert out["condition_trace"][0]["actual"] == "high" and out["condition_trace"][0]["result"] is False
    assert out["action_results"][0]["status"] == "would_run"
    assert await count(dorg.tenant_id, "workflow_executions") == 0
    other = await other_dorg.admin.client.post(f"{WORKFLOW}/dry-run", json=body, headers=h(other_dorg.admin))
    assert other.status_code == 404


async def test_events_never_run_other_tenants_rules(dorg: DOrg, other_dorg: DOrg) -> None:
    await _rule(other_dorg.tenant_id, other_dorg.admin.user_id, "demand.created", [])
    await create_demand(dorg.admin, dorg.client_id)
    assert await count(other_dorg.tenant_id, "workflow_executions") == 0


async def test_snapshot_of_missing_entity(dorg: DOrg) -> None:
    from tests.modules.demands.helpers import admin_ctx

    async with tenant_session(dorg.tenant_id) as db:
        assert await engine.load_snapshot(admin_ctx(dorg), db, "demand", uuid.uuid4()) is None
        assert await engine.load_snapshot(admin_ctx(dorg), db, "application", uuid.uuid4()) is None
