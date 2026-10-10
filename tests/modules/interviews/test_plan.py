"""7.1 plans, kits and templates: seeding (template / legacy jsonb), save rules (409 with a scheduled
interview; archive with completed interviews, stage_name kept), templates, kits versioning."""

import uuid
from datetime import timedelta

from sqlalchemy import update

from app.core.database import tenant_session
from app.models.demands import Demand
from app.models.interviews import Interview, InterviewStage
from tests.modules.demands.helpers import open_demand
from tests.modules.interviews.helpers import API, h, internal, plan, schedule, submitted_app
from tests.modules.pipeline.helpers import POrg


async def test_seed_from_default_template_and_legacy_jsonb(porg: POrg) -> None:
    stages = await plan(porg.admin, porg.demand_id)
    assert [(s["name"], s["kind"]) for s in stages] == [
        ("Technical", "technical"),
        ("Client", "client"),
        ("HR", "hr"),
    ]
    demand = await open_demand(porg.admin, porg.client_id)
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(
            update(Demand)
            .where(Demand.id == uuid.UUID(demand["id"]))
            .values(
                interview_plan=[
                    {"name": "Coding round", "kind": "technical"},
                    {"name": "Culture", "kind": "interview"},
                ]
            )
        )
    legacy = await plan(porg.admin, demand["id"])
    assert [(s["name"], s["kind"]) for s in legacy] == [("Coding round", "technical"), ("Culture", "other")]
    assert legacy[-1]["is_final"] is True


async def test_save_plan_rules(porg: POrg) -> None:
    a = await submitted_app(porg)
    stages = await plan(porg.admin, porg.demand_id)
    tech, client, hr = stages
    iv = (
        await schedule(porg.admin, a["application_id"], client["id"], [internal(porg.admin.user_id)])
    ).json()

    # Removing a stage with a scheduled interview -> 409 with the interview ids.
    keep = [
        {"id": tech["id"], "name": "Technical", "kind": "technical"},
        {"id": hr["id"], "name": "HR", "kind": "hr"},
    ]
    resp = await porg.admin.client.put(
        f"{API}/demands/{porg.demand_id}/interview-plan", json={"stages": keep}, headers=h(porg.admin)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "stage_has_scheduled_interviews"
    assert resp.json()["error"]["details"]["interview_ids"] == [iv["id"]]

    # Once completed, the stage is archived (not deleted) and the interview keeps stage_id / stage_name.
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(
            update(Interview).where(Interview.id == uuid.UUID(iv["id"])).values(status="completed")
        )
    new = [
        {"id": hr["id"], "name": "HR screen", "kind": "hr"},
        {"name": "Final panel", "kind": "managerial", "default_duration_minutes": 90},
        {"id": tech["id"], "name": "Tech deep-dive", "kind": "technical"},
    ]
    resp = await porg.admin.client.put(
        f"{API}/demands/{porg.demand_id}/interview-plan", json={"stages": new}, headers=h(porg.admin)
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["stages"]
    assert [s["name"] for s in out] == ["HR screen", "Final panel", "Tech deep-dive"]
    assert [s["is_final"] for s in out] == [False, False, True]
    assert out[0]["id"] == hr["id"] and out[2]["id"] == tech["id"]
    async with tenant_session(porg.tenant_id) as db:
        archived = await db.get(InterviewStage, uuid.UUID(client["id"]))
        interview = await db.get(Interview, uuid.UUID(iv["id"]))
    assert archived is not None and archived.archived_at is not None
    assert (
        interview is not None
        and interview.stage_id == uuid.UUID(client["id"])
        and interview.stage_name == "Client"
    )

    # A template can only be applied while the plan has no interviews.
    templates = (await porg.admin.client.get(f"{API}/interview-stage-templates")).json()
    resp = await porg.admin.client.post(
        f"{API}/demands/{porg.demand_id}/interview-plan/apply-template",
        json={"template_id": templates[0]["id"]},
        headers=h(porg.admin),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_has_interviews"


async def test_kits_templates_and_apply(porg: POrg) -> None:
    kit_body = {
        "name": "Backend engineer",
        "competencies": [
            {"key": "design", "label": "System design", "guidance": "Trade-offs"},
            {"key": "coding", "label": "Coding"},
        ],
        "questions": [{"key": "q1", "competency_key": "design", "text": "Design a rate limiter"}],
        "rating_scale": 4,
    }
    resp = await porg.admin.client.post(f"{API}/interview-kits", json=kit_body, headers=h(porg.admin))
    assert resp.status_code == 201, resp.text
    kit = resp.json()
    assert kit["version"] == 1 and kit["recommendation_options"] == ["strong_yes", "yes", "no", "strong_no"]
    resp = await porg.admin.client.patch(
        f"{API}/interview-kits/{kit['id']}", json={"name": "Backend engineer v2"}, headers=h(porg.admin)
    )
    assert resp.status_code == 200 and resp.json()["version"] == 2
    bad = await porg.admin.client.post(
        f"{API}/interview-kits",
        json={**kit_body, "questions": [{"key": "q1", "competency_key": "nope", "text": "?"}]},
        headers=h(porg.admin),
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_competency"

    resp = await porg.admin.client.post(
        f"{API}/interview-stage-templates",
        json={
            "name": "Two step",
            "stages": [
                {"name": "Tech", "kind": "technical", "kit_id": kit["id"]},
                {"name": "Client", "kind": "client"},
            ],
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 201, resp.text
    tpl = resp.json()
    resp = await porg.admin.client.post(
        f"{API}/demands/{porg.demand_id}/interview-plan/apply-template",
        json={"template_id": tpl["id"]},
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    stages = resp.json()["stages"]
    assert [s["name"] for s in stages] == ["Tech", "Client"] and stages[0]["kit_id"] == kit["id"]
    assert stages[0]["kit_name"] == "Backend engineer v2"

    settings = await porg.admin.client.get(f"{API}/interviews/settings")
    assert settings.status_code == 200 and settings.json()["reminder_offsets_minutes"] == [1440, 60]
    patched = await porg.admin.client.patch(
        f"{API}/interviews/settings",
        json={"reminder_offsets_minutes": [30, 120], "rating_scale": 4},
        headers=h(porg.admin),
    )
    assert patched.status_code == 200 and patched.json()["reminder_offsets_minutes"] == [120, 30]


async def test_config_cross_tenant(porg: POrg, other_porg: POrg) -> None:
    kit = (
        await porg.admin.client.post(
            f"{API}/interview-kits",
            json={"name": "K", "competencies": [{"key": "a", "label": "A"}]},
            headers=h(porg.admin),
        )
    ).json()
    tpl = (await porg.admin.client.get(f"{API}/interview-stage-templates")).json()[0]
    o = other_porg.admin
    for method, url, payload in (
        ("GET", f"{API}/interview-kits/{kit['id']}", None),
        ("PATCH", f"{API}/interview-kits/{kit['id']}", {"name": "x"}),
        ("POST", f"{API}/interview-kits/{kit['id']}/archive", None),
        ("GET", f"{API}/interview-stage-templates/{tpl['id']}", None),
        ("PATCH", f"{API}/interview-stage-templates/{tpl['id']}", {"name": "x"}),
        ("POST", f"{API}/demands/{porg.demand_id}/interview-plan/apply-template", {"template_id": tpl["id"]}),
    ):
        resp = await o.client.request(method, url, json=payload, headers=h(o))
        assert resp.status_code == 404, (method, url, resp.text)
    # The other tenant's own apply with tenant A's template id -> 404 as well.
    resp = await o.client.post(
        f"{API}/demands/{other_porg.demand_id}/interview-plan/apply-template",
        json={"template_id": tpl["id"]},
        headers=h(o),
    )
    assert resp.status_code == 404
    _ = timedelta
