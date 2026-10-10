"""FR-65 / AD-4: workflow rules can never perform human-only decisions.

* the action registry is closed and holds exactly the seven documented actions;
* human-only actions in rule JSON are refused (422 ``invalid_action``);
* a forged ``move_stage`` to a human-only stage fails with ``transition_not_allowed`` and changes nothing;
* ``app/services/workflow/**`` never evaluates code, uses a template engine, or references the human-only
  service functions.
"""

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.database import tenant_session
from app.services import workflow
from app.services.workflow.actions import ACTIONS, HUMAN_ONLY_STAGES
from tests.epic10 import RULES, DOrg, count, create_demand, h, rows

WORKFLOW_DIR = Path(workflow.__file__).resolve().parent
FORBIDDEN_CALLS = {"eval", "exec", "compile"}
FORBIDDEN_NAMES = {
    "send_submission",
    "release_offer",
    "decide_step",
    "transition_demand",
    "confirm_rejection",
}


def test_action_registry_is_exactly_the_seven_actions() -> None:
    """The seven Phase 1 actions; any other key is a Phase 2 contract action (Epic 32 catalogue)."""
    from app.services.workflow.catalogue import ACTION_CATALOGUE

    p1 = {"assign", "create_task", "notify", "send_email", "move_stage", "add_tag", "refresh_matches"}
    assert p1 <= set(ACTIONS)
    assert set(ACTIONS) - p1 <= set(ACTION_CATALOGUE)


@pytest.mark.parametrize(
    "action", ["reject_application", "send_submission", "release_offer", "approve", "set_demand_status"]
)
async def test_human_only_actions_are_rejected(dorg: DOrg, action: str) -> None:
    resp = await dorg.admin.client.post(
        RULES,
        json={"name": "evil", "trigger": "demand.created", "actions": [{"type": action, "params": {}}]},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_action"
    assert resp.json()["error"]["details"]["path"] == "actions[0].type"


@pytest.mark.parametrize(
    "stage", ["rejected", "submitted", "offered", "hired", "shortlisted", "selected", "withdrawn"]
)
async def test_move_stage_to_human_only_target_is_refused_at_save(dorg: DOrg, stage: str) -> None:
    resp = await dorg.admin.client.post(
        RULES,
        json={
            "name": "evil",
            "trigger": "application.stage_changed",
            "actions": [{"type": "move_stage", "params": {"to_stage_key": stage}}],
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_action"
    assert "transition_not_allowed" in resp.json()["error"]["details"]["reason"]


async def _application(dorg: DOrg) -> tuple[uuid.UUID, str] | None:
    """A real application when the pipeline (Epic 6) is available, else None."""  # noqa: D401
    from app.services import entity_refs

    if entity_refs.optional_service("app.services.pipeline_service") is None:
        return None
    from tests.epic10 import candidate, open_demand

    demand = await open_demand(dorg.admin, dorg.client_id)
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(
        f"/api/v1/demands/{demand['id']}/applications",
        json={"candidate_ids": [cand["id"]], "source": "manual"},
        headers=h(dorg.admin),
    )
    if resp.status_code not in (200, 201):
        return None
    (app_row,) = await rows(
        dorg.tenant_id,
        "SELECT a.id, s.key FROM applications a JOIN pipeline_stages s ON s.id = a.stage_id WHERE a.candidate_id = :c",  # noqa: E501
        c=uuid.UUID(cand["id"]),
    )
    return app_row["id"], app_row["key"]


@pytest.mark.parametrize("stage", ["rejected", "submitted", "offered", "hired"])
async def test_forged_move_stage_fails_and_leaves_stage(dorg: DOrg, stage: str) -> None:
    from app.models.workflow import WorkflowRule

    real = await _application(dorg)
    application_id, before = real if real is not None else (uuid.uuid4(), None)
    rule = WorkflowRule(
        id=uuid.uuid4(), tenant_id=dorg.tenant_id, name="forged", trigger="application.stage_changed",
        conditions={}, actions=[{"type": "move_stage", "params": {"to_stage_key": stage}}],
        is_active=True, version=1, created_by=dorg.admin.user_id,
    )  # fmt: skip
    async with tenant_session(dorg.tenant_id) as db:  # written straight to the DB, bypassing validation
        db.add(rule)
    async with tenant_session(dorg.tenant_id) as db:
        await workflow.evaluate_event(
            db, "application.stage_changed", "application", application_id, uuid.uuid4(), {}, 0
        )
    (execution,) = await rows(
        dorg.tenant_id, "SELECT * FROM workflow_executions WHERE rule_id = :r", r=rule.id
    )
    (result,) = execution["action_results"]
    assert result["status"] == "error" and result["error_code"] == "transition_not_allowed"
    assert execution["status"] == "failed"
    if real is not None:
        (after,) = await rows(
            dorg.tenant_id,
            "SELECT s.key FROM applications a JOIN pipeline_stages s ON s.id = a.stage_id WHERE a.id = :a",
            a=application_id,
        )
        assert after["key"] == before


async def test_dry_run_of_human_only_target_reports_error(dorg: DOrg) -> None:
    from app.services.workflow.actions import ActionContext, MoveStageParams, _move_dry
    from tests.modules.demands.helpers import admin_ctx

    async with tenant_session(dorg.tenant_id) as db:
        ac = ActionContext(
            ctx=admin_ctx(dorg), db=db, tenant_id=dorg.tenant_id, rule_id=None, rule_created_by=dorg.admin.user_id,  # noqa: E501
            trigger="application.stage_changed", entity_type="application", entity_id=uuid.uuid4(),
            snapshot={"stage": "interviewing"}, params=MoveStageParams(to_stage_key="rejected"),
        )  # fmt: skip
        from app.services.workflow.actions import ActionError

        with pytest.raises(ActionError) as exc:
            await _move_dry(ac)
    assert exc.value.code == "transition_not_allowed"
    assert {"rejected", "submitted", "offered", "hired"} <= HUMAN_ONLY_STAGES


async def test_workflow_cannot_change_demand_status(dorg: DOrg) -> None:
    """No action touches demand status: a rule with every demand-capable action leaves the status alone."""
    demand = await create_demand(dorg.admin, dorg.client_id)
    async with tenant_session(dorg.tenant_id) as db:
        await workflow.evaluate_event(
            db, "demand.created", "demand", uuid.UUID(demand["id"]), uuid.uuid4(), {}, 0
        )
    (row,) = await rows(dorg.tenant_id, "SELECT status FROM demands WHERE id = :d", d=uuid.UUID(demand["id"]))
    assert row["status"] == "draft"
    assert await count(dorg.tenant_id, "workflow_executions") == 0  # no active rules


def _sources() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(WORKFLOW_DIR.rglob("*.py"))]


def test_no_dynamic_code_or_template_engine() -> None:
    sources = _sources()
    assert sources
    for path, tree in sources:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, f"{path.name}: {node.func.id}()"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN_CALLS, f"{path.name}: .{node.func.attr}()"
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "jinja2" for a in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "jinja2", path.name
        text = path.read_text(encoding="utf-8")
        for token in ("eval(", "exec(", "compile(", "jinja2"):
            assert token not in text.replace("evaluate(", "").replace("_eval", ""), f"{path.name}: {token}"


def test_import_graph_never_reaches_human_only_functions() -> None:
    for path, tree in _sources():
        for node in ast.walk(tree):
            names: list[Any] = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name.rsplit(".", 1)[-1] for a in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            hit = FORBIDDEN_NAMES & set(names)
            assert not hit, f"{path.name} references {hit}"


async def test_move_stage_to_allowed_custom_stage_runs(dorg: DOrg) -> None:
    """The positive path: applied -> custom (``screening``) is open to ``workflow`` in the pipeline table."""
    from app.models.workflow import WorkflowRule

    real = await _application(dorg)
    if real is None:
        pytest.skip("pipeline (Epic 6) not available")
    application_id, before = real
    assert before == "applied"
    rule = WorkflowRule(
        id=uuid.uuid4(),
        tenant_id=dorg.tenant_id,
        name="to screening",
        trigger="application.stage_changed",
        conditions={},
        actions=[{"type": "move_stage", "params": {"to_stage_key": "screening"}}],
        is_active=True,
        version=1,
        created_by=dorg.admin.user_id,
    )
    async with tenant_session(dorg.tenant_id) as db:
        db.add(rule)
    async with tenant_session(dorg.tenant_id) as db:
        await workflow.evaluate_event(
            db, "application.stage_changed", "application", application_id, uuid.uuid4(), {}, 0
        )
    runs = await rows(
        dorg.tenant_id,
        "SELECT * FROM workflow_executions WHERE rule_id = :r ORDER BY chain_depth, created_at",
        r=rule.id,
    )
    assert runs[0]["chain_depth"] == 0 and runs[0]["status"] == "succeeded", runs[0]["action_results"]
    # the move re-triggers the same rule (chain depth 1): loop protection stops it
    assert all(r["status"] in ("skipped_rate_limit", "skipped_loop") for r in runs[1:])
    (after,) = await rows(
        dorg.tenant_id,
        "SELECT s.key FROM applications a JOIN pipeline_stages s ON s.id = a.stage_id WHERE a.id = :a",
        a=application_id,
    )
    assert after["key"] == "screening"
