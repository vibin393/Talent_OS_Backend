"""Workflow rule administration (FR-58/59): validation, lifecycle, limits, registry, permissions, tenancy."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.services.workflow import rules as rules_service
from tests.epic10 import RULES, WORKFLOW, DOrg, h, member


def rule_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": f"High priority {uuid.uuid4().hex[:6]}",
        "trigger": "demand.status_changed",
        "conditions": {
            "all": [
                {"field": "event.to_status", "op": "eq", "value": "open"},
                {"field": "priority", "op": "eq", "value": "high"},
            ]
        },
        "actions": [
            {"type": "create_task", "params": {"title": "Kick-off {{ demand.human_id }}", "owner": "entity_owner", "due_in_hours": 4}},  # noqa: E501
            {"type": "notify", "params": {"recipients": ["entity_owner"], "message": "New high-priority demand"}},  # noqa: E501
        ],
    }  # fmt: skip
    body.update(overrides)
    return body


async def _create(m: Any, **overrides: Any) -> dict[str, Any]:
    resp = await m.client.post(RULES, json=rule_body(**overrides), headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_new_rule_is_inactive_even_if_asked(dorg: DOrg) -> None:
    rule = await _create(dorg.admin, is_active=True)
    assert rule["is_active"] is False and rule["version"] == 1
    assert rule["actions"][0]["params"]["priority"] == "normal"  # normalised with defaults


async def test_lifecycle_and_version(dorg: DOrg) -> None:
    rule = await _create(dorg.admin)
    rid = rule["id"]
    resp = await dorg.admin.client.patch(f"{RULES}/{rid}", json={"name": "Renamed"}, headers=h(dorg.admin))
    assert (
        resp.status_code == 200
        and resp.json()["version"] == 2
        and resp.json()["updated_by"] == str(dorg.admin.user_id)
    )
    same = await dorg.admin.client.patch(f"{RULES}/{rid}", json={"name": "Renamed"}, headers=h(dorg.admin))
    assert same.json()["version"] == 2
    assert (await dorg.admin.client.post(f"{RULES}/{rid}/activate", headers=h(dorg.admin))).json()[
        "is_active"
    ] is True
    assert (await dorg.admin.client.post(f"{RULES}/{rid}/deactivate", headers=h(dorg.admin))).json()[
        "is_active"
    ] is False
    archived = await dorg.admin.client.post(f"{RULES}/{rid}/archive", headers=h(dorg.admin))
    assert archived.json()["archived_at"] is not None
    assert (await dorg.admin.client.post(f"{RULES}/{rid}/activate", headers=h(dorg.admin))).status_code == 409
    listed = await dorg.admin.client.get(RULES)
    assert rid not in {r["id"] for r in listed.json()["items"]}
    execs = await dorg.admin.client.get(f"{RULES}/{rid}/executions")
    assert execs.status_code == 200 and execs.json()["total"] == 0


async def test_rule_limit(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules_service, "MAX_ACTIVE_RULES", 1)
    first, second = await _create(dorg.admin), await _create(dorg.admin)
    assert (
        await dorg.admin.client.post(f"{RULES}/{first['id']}/activate", headers=h(dorg.admin))
    ).status_code == 200
    resp = await dorg.admin.client.post(f"{RULES}/{second['id']}/activate", headers=h(dorg.admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "rule_limit"


@pytest.mark.parametrize(
    ("overrides", "code", "path"),
    [
        ({"trigger": "demand.deleted"}, "invalid_trigger", "trigger"),
        ({"conditions": {"all": [{"field": "bill_rate", "op": "gt", "value": 1}]}}, "invalid_condition", "conditions.all[0].field"),  # noqa: E501
        ({"conditions": {"all": [{"field": "priority", "op": "eq", "value": "urgent"}]}}, "invalid_condition", "conditions.all[0].value"),  # noqa: E501
        ({"actions": [{"type": "move_stage", "params": {"to_stage_key": "interviewing"}}]}, "invalid_action", "actions[0]"),  # noqa: E501
        ({"actions": [{"type": "create_task", "params": {"title": ""}}]}, "invalid_action", "actions[0].params.title"),  # noqa: E501
        ({"actions": [{"type": "create_task", "params": {"title": "x", "owner": "boss"}}]}, "invalid_action", "actions[0].params.owner"),  # noqa: E501
        ({"actions": [{"type": "notify", "params": {"recipients": [], "message": "x"}}]}, "invalid_action", "actions[0].params.recipients"),  # noqa: E501
        ({"actions": [{"type": "create_task", "params": {"title": "x"}}] * 11}, "invalid_action", "actions"),
        ({"trigger": "candidate.created", "conditions": {}, "actions": [{"type": "assign", "params": {"user_ids": [str(uuid.uuid4())]}}]}, "invalid_action", "actions[0]"),  # noqa: E501
    ],
)  # fmt: skip
async def test_invalid_rules_are_422_with_path(
    dorg: DOrg, overrides: dict[str, Any], code: str, path: str
) -> None:
    resp = await dorg.admin.client.post(RULES, json=rule_body(**overrides), headers=h(dorg.admin))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code
    assert resp.json()["error"]["details"]["path"] == path


async def test_registry(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(f"{WORKFLOW}/registry")
    assert resp.status_code == 200
    body = resp.json()
    triggers = {t["key"]: t for t in body["triggers"]}
    assert {
        "demand.created",
        "demand.status_changed",
        "application.stage_changed",
        "candidate.created",
    } <= set(triggers)
    fields = {f["path"]: f for f in triggers["demand.status_changed"]["fields"]}
    assert fields["event.to_status"]["type"] == "enum" and "open" in fields["event.to_status"]["values"]
    assert "bill_rate" not in fields
    # Phase 1's seven actions; Phase 2 contract actions join as their epics register them (Epic 32).
    assert {
        "assign", "create_task", "notify", "send_email", "move_stage", "add_tag", "refresh_matches",
    } <= {a["type"] for a in body["actions"]}  # fmt: skip
    assert "rejected" in body["move_stage_blocked_targets"]


async def test_permissions(app: FastAPI, dorg: DOrg) -> None:
    rec = await member(app, dorg, "recruiter")
    assert (await rec.client.get(RULES)).status_code == 403
    assert (await rec.client.post(RULES, json=rule_body(), headers=h(rec))).status_code == 403
    await rec.client.aclose()


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("patch", "", {"name": "x"}),
        ("post", "/activate", None),
        ("post", "/deactivate", None),
        ("post", "/archive", None),
        ("get", "/executions", None),
        ("post", "/dry-run", {"entity_id": str(uuid.uuid4())}),
    ],
)
async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg, method: str, suffix: str, body: Any) -> None:
    rule = await _create(dorg.admin)
    kwargs: dict[str, Any] = {"headers": h(other_dorg.admin)}
    if body is not None:
        kwargs["json"] = body
    resp = await getattr(other_dorg.admin.client, method)(f"{RULES}/{rule['id']}{suffix}", **kwargs)
    assert resp.status_code == 404, resp.text
    missing = await other_dorg.admin.client.get(f"{WORKFLOW}/executions/{uuid.uuid4()}")
    assert missing.status_code == 404
