# ruff: noqa: E501
"""Epic 32 story 19.3 (FR-169, AD-31): workflow v2 graphs -- validation, P1 compatibility, outline, live runs
with delay / approval waits, pinned versions, cancellation and dry runs."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.workflow import WorkflowExecution, WorkflowWait
from app.schemas.approvals import DecideIn
from app.services import approvals
from app.services.workflow import engine, waits
from app.services.workflow import graph as graphs
from tests.epic10 import RULES, DOrg, count, create_demand, h
from tests.sla.helpers import admin_ctx

TRIGGER = "demand.created"


def _action(i: int) -> dict[str, Any]:
    return {
        "id": f"a{i}",
        "type": "action",
        "action": {"type": "notify", "params": {"recipients": ["entity_owner"], "message": f"m{i}"}},
        "next": f"a{i + 1}",
    }


def _err(raw: Any, trigger: str = TRIGGER) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as exc:
        graphs.validate_graph(raw, trigger)
    return exc.value


# ================================================================ validation matrix
def test_validation_matrix() -> None:
    end = {"id": "e", "type": "end"}
    cycle = {
        "start": "a1",
        "nodes": [
            {
                "id": "a1",
                "type": "action",
                "action": {"type": "notify", "params": {"recipients": ["entity_owner"], "message": "x"}},
                "next": "d1",
            },
            {"id": "d1", "type": "delay", "duration": {"value": 1, "unit": "hours"}, "next": "a1"},
        ],
    }
    err = _err(cycle)
    assert err.code == "invalid_graph" and err.details == {"path": "nodes[1].next", "reason": "cycle"}

    unreachable = {
        "start": "e",
        "nodes": [end, {"id": "x", "type": "delay", "duration": {"value": 1, "unit": "hours"}, "next": "e"}],
    }
    assert _err(unreachable).details == {"path": "nodes[1]", "reason": "unreachable"}

    delays = [
        {
            "id": f"d{i}",
            "type": "delay",
            "duration": {"value": 1, "unit": "hours"},
            "next": f"d{i + 1}" if i < 26 else "e",
        }
        for i in range(1, 27)
    ]
    assert _err({"start": "d1", "nodes": [*delays, end]}).details["reason"] == "too_many_steps"

    eleven = [_action(i) for i in range(1, 12)]
    eleven[-1]["next"] = "e"
    assert _err({"start": "a1", "nodes": [*eleven, end]}).details["reason"] == "too_many_actions"

    unknown = {
        "start": "a1",
        "nodes": [{"id": "a1", "type": "action", "action": {"type": "approve", "params": {}}, "next": None}],
    }
    err = _err(unknown)
    assert err.code == "invalid_action" and err.details["path"] == "nodes[0].action.type"

    too_long = {
        "start": "d1",
        "nodes": [{"id": "d1", "type": "delay", "duration": {"value": 91, "unit": "days"}, "next": None}],
    }
    assert _err(too_long).details == {"path": "nodes[0].duration", "reason": "delay_too_long"}
    too_short = {
        "start": "d1",
        "nodes": [{"id": "d1", "type": "delay", "duration": {"value": 2, "unit": "minutes"}, "next": None}],
    }
    assert _err(too_short).details["reason"] == "delay_too_short"

    star = {
        "start": "a1",
        "nodes": [
            {
                "id": "a1",
                "type": "action",
                "action": {"type": "move_stage", "params": {"to_stage_key": "hired"}},
                "next": None,
            }
        ],
    }
    err = _err(star, "application.stage_changed")
    assert err.code == "invalid_action" and err.details == {
        "path": "nodes[0].action",
        "reason": "human_only_target",
    }

    no_branch = {
        "start": "p",
        "nodes": [{"id": "p", "type": "approval", "title": "t", "approver": {"kind": "entity_owner"}}],
    }
    assert _err(no_branch).details["reason"] == "missing_branch"
    no_ref = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "approval", "title": "t", "approver": {"kind": "role"}, "on_approved": "e"},
            end,
        ],
    }
    assert _err(no_ref).details["reason"] == "approver_unresolved"
    bad_ref = {"start": "a1", "nodes": [{**_action(1), "next": "zz"}]}
    assert _err(bad_ref).details == {"path": "nodes[0].next", "reason": "unknown_node"}
    bad_cond = {
        "start": "c",
        "nodes": [
            {"id": "c", "type": "condition", "when": {"field": "nope", "op": "eq", "value": 1}, "then": None}
        ],
    }
    assert _err(bad_cond).code == "invalid_condition"


P1_FIXTURES: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [
    ({}, []),
    ({}, [{"type": "notify", "params": {"recipients": ["entity_owner"], "message": "hi"}}]),
    (
        {"field": "priority", "op": "eq", "value": "high"},
        [
            {
                "type": "create_task",
                "params": {
                    "title": "Kick-off",
                    "owner": "primary_recruiter",
                    "due_in_hours": 4,
                    "priority": "high",
                    "reminder_before_minutes": None,
                },
            }
        ],
    ),
    (
        {
            "all": [
                {"field": "priority", "op": "in", "value": ["high", "critical"]},
                {"field": "positions", "op": "gte", "value": 2},
            ]
        },
        [
            {"type": "notify", "params": {"recipients": ["primary_recruiter"], "message": "a"}},
            {"type": "refresh_matches", "params": {}},
        ],
    ),
    ({"field": "priority", "op": "eq", "value": "low"}, []),
]


@pytest.mark.parametrize(("cond", "actions"), P1_FIXTURES)
def test_from_p1_to_p1_round_trip(cond: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    from app.services.workflow.rules import _validate

    cond_n, actions_n = _validate(TRIGGER, cond, actions)
    graph = graphs.from_p1(cond_n, actions_n)
    normalized = graphs.validate_graph(graph, TRIGGER)
    assert graphs.to_p1(normalized) == (cond_n, actions_n)
    rows = graphs.outline(normalized, TRIGGER)
    assert rows[0]["depth"] == 0
    if cond:
        assert rows[0]["text"].startswith("If ")


def test_to_p1_refuses_non_linear_graphs() -> None:
    graph = {
        "start": "c",
        "nodes": [
            {"id": "c", "type": "condition", "when": {}, "then": "e", "else": "a1"},
            {**_action(1), "next": "e"},
            {"id": "e", "type": "end"},
        ],
    }
    assert graphs.to_p1(graphs.validate_graph(graph, TRIGGER)) is None
    delayed = {
        "start": "d",
        "nodes": [{"id": "d", "type": "delay", "duration": {"value": 1, "unit": "hours"}, "next": None}],
    }
    assert graphs.to_p1(graphs.validate_graph(delayed, TRIGGER)) is None


# ================================================================ API: storage form
async def test_linear_graph_saved_as_p1_and_p1_rule_round_trips(dorg: DOrg) -> None:
    a = dorg.admin
    graph = graphs.from_p1(
        {"field": "priority", "op": "eq", "value": "high"},
        [{"type": "notify", "params": {"recipients": ["entity_owner"], "message": "x"}}],
    )
    resp = await a.client.post(
        RULES, json={"name": "Linear", "trigger": TRIGGER, "graph": graph}, headers=h(a)
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["is_p1_form"] is True and rule["definition_version"] == 1
    assert (
        rule["conditions"] == {"field": "priority", "op": "eq", "value": "high"} and len(rule["actions"]) == 1
    )
    assert rule["outline"][0]["text"] == "If Priority is high"
    assert rule["graph"]["start"] == "c1" and rule["step_count"] == 2

    # opening and saving the derived graph unchanged is a no-op (no new version)
    again = await a.client.patch(f"{RULES}/{rule['id']}", json={"graph": rule["graph"]}, headers=h(a))
    assert again.status_code == 200 and again.json()["version"] == 1

    both = await a.client.post(
        RULES,
        json={"name": "Both", "trigger": TRIGGER, "graph": graph, "actions": rule["actions"]},
        headers=h(a),
    )
    assert both.status_code == 422 and both.json()["error"]["code"] == "graph_and_form"
    assert await count(dorg.tenant_id, "workflow_rule_versions", "rule_id = :r", r=uuid.UUID(rule["id"])) == 1


def _approval_graph(approver: uuid.UUID, title: str = "Rate follow-up") -> dict[str, Any]:
    return {
        "start": "c1",
        "nodes": [
            {"id": "c1", "type": "condition", "when": {"field": "priority", "op": "eq", "value": "high"}, "then": "d1", "else": "e"},
            {"id": "d1", "type": "delay", "duration": {"value": 1, "unit": "business_days"}, "next": "p1"},
            {
                "id": "p1", "type": "approval", "title": "Approve the follow-up",
                "approver": {"kind": "user", "ref": str(approver)},
                "timeout": {"value": 5, "unit": "business_days"},
                "on_approved": "a1", "on_rejected": "a2", "on_timeout": "e",
            },
            {"id": "a1", "type": "action", "action": {"type": "create_task", "params": {"title": title, "owner": f"user:{approver}"}}, "next": "e"},
            {"id": "a2", "type": "action", "action": {"type": "notify", "params": {"recipients": [f"user:{approver}"], "message": "Follow-up rejected"}}, "next": "e"},
            {"id": "e", "type": "end"},
        ],
        "layout": {"c1": {"x": 0, "y": 0}},
    }  # fmt: skip


async def _graph_rule(dorg: DOrg, graph: dict[str, Any]) -> dict[str, Any]:
    a = dorg.admin
    resp = await a.client.post(
        RULES,
        json={"name": f"Graph {uuid.uuid4().hex[:5]}", "trigger": TRIGGER, "graph": graph},
        headers=h(a),
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["is_p1_form"] is False and rule["definition_version"] == 2
    assert (await a.client.post(f"{RULES}/{rule['id']}/activate", headers=h(a))).status_code == 200
    return rule


async def _waits(tenant_id: uuid.UUID, rule_id: str) -> list[WorkflowWait]:
    async with tenant_session(tenant_id) as db:
        out = list(
            (
                await db.execute(
                    select(WorkflowWait)
                    .where(WorkflowWait.rule_id == uuid.UUID(rule_id))
                    .order_by(WorkflowWait.created_at)
                )
            ).scalars()
        )
        for w in out:
            db.expunge(w)
        return out


async def _execution(tenant_id: uuid.UUID, execution_id: uuid.UUID) -> WorkflowExecution:
    async with tenant_session(tenant_id) as db:
        row = await db.get(WorkflowExecution, execution_id)
        assert row is not None
        db.expunge(row)
        return row


async def _resume(tenant_id: uuid.UUID, at: datetime) -> int:
    async with tenant_session(tenant_id) as db:
        return await waits.resume_waits(db, now=at)


async def _tasks(tenant_id: uuid.UUID, title: str) -> int:
    return await count(tenant_id, "tasks", "title = :t", t=title)


async def test_graph_rule_end_to_end_approved_with_pinned_version(dorg: DOrg) -> None:
    a = dorg.admin
    rule = await _graph_rule(dorg, _approval_graph(a.user_id))
    assert [r["text"] for r in rule["outline"]][:3] == ["If Priority is high", "Yes:", "Wait 1 business day"]
    low = await create_demand(a, dorg.client_id, priority="low")
    demand = await create_demand(a, dorg.client_id, priority="high")
    [wait] = await _waits(dorg.tenant_id, rule["id"])
    assert wait.kind == "delay" and wait.status == "waiting" and wait.entity_id == uuid.UUID(demand["id"])
    execution = await _execution(dorg.tenant_id, wait.execution_id)
    assert execution.status == "waiting" and execution.rule_version == 1
    not_matched = await count(
        dorg.tenant_id,
        "workflow_executions",
        "entity_id = :e AND status = 'not_matched'",
        e=uuid.UUID(low["id"]),
    )
    assert not_matched == 1

    # editing the rule never changes the pinned version of the waiting run
    edited = _approval_graph(a.user_id, title="Edited title")
    resp = await a.client.patch(f"{RULES}/{rule['id']}", json={"graph": edited}, headers=h(a))
    assert resp.status_code == 200 and resp.json()["version"] == 2

    assert await _resume(dorg.tenant_id, wait.resume_at - timedelta(minutes=1)) == 0  # type: ignore[operator]
    assert await _resume(dorg.tenant_id, wait.resume_at + timedelta(minutes=1)) == 1  # type: ignore[operator]
    delay, approval = await _waits(dorg.tenant_id, rule["id"])
    assert delay.status == "resumed" and approval.kind == "approval" and approval.status == "waiting"
    request_id = approval.approval_request_id
    assert request_id is not None
    ctx = await admin_ctx(dorg.tenant_id, a.user_id)
    mine = await a.client.get("/api/v1/approvals/mine")
    assert any(
        r["id"] == str(request_id) and r["subject_type"] == "workflow_step" for r in mine.json()["items"]
    )

    async with tenant_session(dorg.tenant_id) as db:
        await approvals.decide(ctx, db, request_id, DecideIn(action="approve"))
        # the decision transaction runs no actions
        assert await db.scalar(select(WorkflowWait.status).where(WorkflowWait.id == approval.id)) == "resumed"
        assert (
            await db.execute(
                select(WorkflowExecution.status).where(WorkflowExecution.id == approval.execution_id)
            )
        ).scalar() == "waiting"
    # after the commit the run continued on version 1
    assert await _tasks(dorg.tenant_id, "Rate follow-up") == 1
    assert await _tasks(dorg.tenant_id, "Edited title") == 0
    execution = await _execution(dorg.tenant_id, approval.execution_id)
    assert execution.status == "succeeded"
    assert [n["node_id"] for n in execution.node_trace] == ["c1", "d1", "d1", "p1", "p1", "a1"]
    # the rule never changed the demand status
    assert (await a.client.get(f"/api/v1/demands/{demand['id']}")).json()["status"] == demand["status"]


async def test_rejected_and_timeout_branches(dorg: DOrg) -> None:
    a = dorg.admin
    rule = await _graph_rule(dorg, _approval_graph(a.user_id))
    await create_demand(a, dorg.client_id, priority="high")
    await create_demand(a, dorg.client_id, priority="high")
    first, second = await _waits(dorg.tenant_id, rule["id"])
    await _resume(dorg.tenant_id, max(first.resume_at, second.resume_at) + timedelta(minutes=1))  # type: ignore[type-var,operator]
    approvals_waits = [w for w in await _waits(dorg.tenant_id, rule["id"]) if w.kind == "approval"]
    assert len(approvals_waits) == 2
    reject, timeout = approvals_waits
    ctx = await admin_ctx(dorg.tenant_id, a.user_id)
    async with tenant_session(dorg.tenant_id) as db:
        await approvals.decide(ctx, db, reject.approval_request_id, DecideIn(action="reject", comment="no"))  # type: ignore[arg-type]
    assert (
        await count(
            dorg.tenant_id, "notifications", "type = 'workflow_notice' AND title = 'Follow-up rejected'"
        )
        == 1
    )
    assert await _tasks(dorg.tenant_id, "Rate follow-up") == 0

    assert timeout.timeout_at is not None
    assert await _resume(dorg.tenant_id, timeout.timeout_at + timedelta(minutes=1)) == 1
    [_, timed] = [w for w in await _waits(dorg.tenant_id, rule["id"]) if w.kind == "approval"]
    assert timed.status == "timed_out" and timed.outcome == "timeout"
    status = await count(
        dorg.tenant_id, "approval_requests", "id = :r AND status = 'cancelled'", r=timed.approval_request_id
    )
    assert status == 1
    assert (await _execution(dorg.tenant_id, timed.execution_id)).status == "succeeded"


async def test_deactivation_cancels_waits_and_approvals(dorg: DOrg) -> None:
    a = dorg.admin
    graph = _approval_graph(a.user_id)
    graph["start"] = "p1"  # straight to the approval
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in ("c1", "d1")]
    rule = await _graph_rule(dorg, graph)
    await create_demand(a, dorg.client_id, priority="high")
    [wait] = await _waits(dorg.tenant_id, rule["id"])
    assert wait.kind == "approval" and wait.approval_request_id is not None
    resp = await a.client.post(f"{RULES}/{rule['id']}/deactivate", headers=h(a))
    assert resp.status_code == 200 and resp.json()["waiting_runs"] == 0
    [wait] = await _waits(dorg.tenant_id, rule["id"])
    assert wait.status == "cancelled"
    assert (
        await count(
            dorg.tenant_id,
            "approval_requests",
            "id = :r AND status = 'cancelled'",
            r=wait.approval_request_id,
        )
        == 1
    )
    assert (await _execution(dorg.tenant_id, wait.execution_id)).status == "cancelled"
    executions = await a.client.get(f"/api/v1/workflow/executions/{wait.execution_id}")
    assert executions.json()["waits"][0]["status"] == "cancelled"


async def test_dry_run_paths_with_assume_approval(dorg: DOrg) -> None:
    a = dorg.admin
    rule = await _graph_rule(dorg, _approval_graph(a.user_id))
    await a.client.post(f"{RULES}/{rule['id']}/deactivate", headers=h(a))
    demand = await create_demand(a, dorg.client_id, priority="high")
    before = await count(dorg.tenant_id, "workflow_waits")
    out: dict[str, Any] = {}
    for assume in ("approved", "rejected", "timeout"):
        resp = await a.client.post(
            f"{RULES}/{rule['id']}/dry-run",
            json={"entity_id": demand["id"], "assume_approval": assume},
            headers=h(a),
        )
        assert resp.status_code == 200, resp.text
        out[assume] = resp.json()
    path = [(p["node_id"], p["outcome"]) for p in out["approved"]["path"]]
    assert path == [("c1", "yes"), ("d1", "wait"), ("p1", "approved"), ("a1", "would_run"), ("e", "end")]
    assert out["approved"]["path"][1]["scheduled_at"] is not None
    assert out["approved"]["path"][1]["scheduled_at"].endswith("+05:30")  # tenant timezone
    assert [p["node_id"] for p in out["rejected"]["path"]][3] == "a2"
    timeout_path = out["timeout"]["path"]
    assert timeout_path[2]["outcome"] == "timeout" and timeout_path[2]["scheduled_at"] is not None
    assert await count(dorg.tenant_id, "workflow_waits") == before
    assert await count(dorg.tenant_id, "approval_requests", "subject_type = 'workflow_step'") == 0


async def test_loop_protection_applies_to_graph_rules(dorg: DOrg) -> None:
    a = dorg.admin
    rule = await _graph_rule(dorg, _approval_graph(a.user_id))
    demand = await create_demand(a, dorg.client_id, priority="low")
    async with tenant_session(dorg.tenant_id) as db:
        ids = await engine.evaluate_event(
            db, TRIGGER, "demand", uuid.UUID(demand["id"]), uuid.uuid4(), {}, chain_depth=9
        )
    statuses = {(await _execution(dorg.tenant_id, i)).status for i in ids}
    assert statuses == {"skipped_loop"}
    assert rule["id"]


async def test_create_task_escalation_policy_must_exist(dorg: DOrg) -> None:
    a = dorg.admin
    body = {
        "name": "Task with escalation",
        "trigger": TRIGGER,
        "actions": [
            {
                "type": "create_task",
                "params": {
                    "title": "x",
                    "escalation_policy_id": str(uuid.uuid4()),
                    "due_in": {"value": 2, "unit": "business_days"},
                },
            }
        ],
    }
    resp = await a.client.post(RULES, json=body, headers=h(a))
    assert (
        resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "unknown_escalation_policy"
    )
    policy = await a.client.post(
        "/api/v1/sla/escalation-policies",
        json={"name": "Task policy", "after_value": "4", "target_kind": "assignee_manager"},
        headers=h(a),
    )
    body["actions"][0]["params"]["escalation_policy_id"] = policy.json()["id"]
    ok = await a.client.post(RULES, json=body, headers=h(a))
    assert ok.status_code == 201, ok.text
    assert ok.json()["actions"][0]["params"]["due_in"] == {"value": 2, "unit": "business_days"}
    reg = await a.client.get("/api/v1/workflow/registry")
    assert {"id": policy.json()["id"], "name": "Task policy"} in reg.json()["escalation_policies"]
    assert reg.json()["max_steps"] == 25 and "approval" in reg.json()["node_types"]
    _ = datetime.now(UTC)
