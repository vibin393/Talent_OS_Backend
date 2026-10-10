# ruff: noqa: E501
"""Epic 48 (A6, AD-45, decision 9): ``analytics.prediction_band_changed`` allows only notify / create_task; save and
run refuse every other action; existing triggers are unchanged."""

import uuid
from typing import Any

from app.core.database import tenant_session
from app.services.workflow import engine
from app.services.workflow.registry import allowed_actions_for
from tests.modules.analytics.helpers import API
from tests.modules.demands.helpers import DOrg, h

RULES = f"{API}/workflow/rules"
TRIGGER = "analytics.prediction_band_changed"
HIGH = {"all": [{"field": "event.to_band", "op": "eq", "value": "high"}]}


def body(*actions: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"Band high {uuid.uuid4().hex[:6]}",
        "trigger": TRIGGER,
        "conditions": HIGH,
        "actions": list(actions),
    }


async def test_save_refuses_other_actions(dorg: DOrg) -> None:
    a = dorg.admin
    for action in (
        {"type": "move_stage", "params": {"to_stage": "rejected"}},
        {"type": "send_email", "params": {"to": "entity_owner", "subject": "x", "body": "y"}},
        {"type": "add_tag", "params": {"tags": ["risky"]}},
    ):
        r = await a.client.post(RULES, json=body(action), headers=h(a))
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] in (
            "action_not_allowed_for_trigger",
            "invalid_actions",
            "validation_failed",
        ), r.text
    r = await a.client.post(
        RULES, json=body({"type": "move_stage", "params": {"to_stage": "hired"}}), headers=h(a)
    )
    assert r.status_code == 422  # human-only suite case: a prediction can never perform a starred transition


async def test_notify_and_task_with_escalation_are_allowed(dorg: DOrg) -> None:
    a = dorg.admin
    policy = await a.client.post(
        f"{API}/sla/escalation-policies",
        json={"name": "Risk escalation", "after_value": "4", "target_kind": "assignee_manager"},
        headers=h(a),
    )
    assert policy.status_code == 201, policy.text
    r = await a.client.post(
        RULES,
        json=body(
            {"type": "notify", "params": {"recipients": ["entity_owner"], "message": "Risk is now high"}},
            {
                "type": "create_task",
                "params": {
                    "title": "Review the risk",
                    "owner": "entity_owner",
                    "due_in_hours": 24,
                    "escalation_policy_id": policy.json()["id"],
                },
            },
        ),
        headers=h(a),
    )
    assert r.status_code == 201, r.text
    reg = (await a.client.get(f"{API}/workflow/registry")).json()
    trig = next(t for t in reg["triggers"] if t["key"] == TRIGGER)
    assert trig["allowed_actions"] == ["create_task", "notify"]
    assert {f["path"] for f in trig["fields"]} >= {
        "model_kind",
        "subject_kind",
        "band",
        "score",
        "event.to_band",
    }
    assert next(t for t in reg["triggers"] if t["key"] == "demand.status_changed")["allowed_actions"] is None


async def test_run_refuses_other_actions(dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        results = await engine.run_actions(
            {"db": db, "trigger": TRIGGER},
            [{"type": "move_stage", "params": {"to_stage": "rejected"}}],
            dry=False,
        )
    assert results == [
        {
            "index": 0,
            "type": "move_stage",
            "status": "error",
            "detail": "Action not allowed for this trigger",
            "error_code": "action_not_allowed_for_trigger",
        }
    ]


def test_existing_triggers_unaffected() -> None:
    assert allowed_actions_for(TRIGGER) == frozenset({"notify", "create_task"})
    for key in ("demand.status_changed", "application.stage_changed", "placements.status_changed"):
        assert allowed_actions_for(key) is None
