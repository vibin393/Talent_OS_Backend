# ruff: noqa: E501
"""Epic 78 stories 78.1 / 78.2 (FR-401, FR-402): feature gate, limits, template rule, one open request, budget,
the agent run (subject, read tools only) -> drafts per segment x variant, invalid result -> failed + fallback, and the
78.1 AC: no recipient record ever reaches the model (gateway payload scan)."""

import json
import re
import uuid

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, ValidationFailed
from app.core.permissions import Principal, Scope
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.services.ai.fake_llm import FakeTurn
from app.services.campaign_copy.common import FALLBACK_MESSAGE
from tests.campaign_copy.helpers import SEEDED, body, detail, request, request_row, script, setup
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg


async def test_feature_off_is_409(dorg: DOrg) -> None:
    env = await setup(dorg, feature=False)
    with pytest.raises(ConflictError) as exc:
        await request(env, body(env))
    assert exc.value.code == "ai_feature_disabled"
    assert "AI campaign copy is off" in exc.value.message


async def test_limits_are_422_with_maxima(dorg: DOrg) -> None:
    env = await setup(dorg)
    with pytest.raises(ValidationFailed) as exc:
        await request(env, body(env, segments=[
            {"key": f"x_{i}", "label": f"Segment {i}", "source": {"kind": "attribute", "field": "city", "value": "Pune"}} for i in range(21)
        ]))
    assert exc.value.code == "copy_request_too_large"
    assert exc.value.details == {"max_segments": 20, "max_variants": 3}
    with pytest.raises(ValidationFailed) as exc:
        await request(env, body(env, variants_per_segment=4))
    assert exc.value.code == "copy_request_too_large"


async def test_sms_needs_an_approved_template(dorg: DOrg) -> None:
    from tests.modules.campaigns.helpers import API

    env = await setup(dorg)
    resp = await env.m.client.post(
        f"{API}/campaigns", json={"name": "SMS push", "channel": "sms", "audience_id": str(env.audience_id)}, headers=h(env.m)
    )
    assert resp.status_code == 201, resp.text
    data = body(env, campaign_id=resp.json()["id"], channel="sms")
    with pytest.raises(ValidationFailed) as exc:
        await request(env, data)
    assert exc.value.code == "template_required"


async def test_run_creates_checked_drafts_per_segment_and_variant(dorg: DOrg) -> None:
    env = await setup(dorg)
    out, fake = await request(env, body(env))
    assert out.status == "generating" and out.agent_run_id is not None
    row = await request_row(env, out.id)
    assert row.status == "generated", (row.status, row.error_code)
    got = await detail(env, out.id)
    assert got.summary == {"segments": 2, "variants": 4, "blocked": 0}
    assert [len(s.drafts) for s in got.segments] == [2, 2]
    assert all(d.status == "checked" and d.ai_generated for s in got.segments for d in s.drafts)
    async with tenant_session(env.tenant_id) as db:
        run = await db.get(AgentRun, out.agent_run_id)
        calls = list((await db.execute(select(AgentToolCall).where(AgentToolCall.run_id == run.id))).scalars())
    assert run.subject_type == "campaign" and run.subject_id == env.campaign_id and run.agent_key == "campaign_copy"
    assert {c.tool_key for c in calls} == {
        "campaign_copy.get_brief", "campaign_copy.get_postings", "campaign_copy.get_templates", "campaign_copy.get_tenant_style",
    }  # fmt: skip
    assert all(c.outcome == "executed" for c in calls), [(c.tool_key, c.outcome, c.error_code) for c in calls]
    assert len(fake.calls) == 3


async def test_one_open_request_per_campaign(dorg: DOrg) -> None:
    from app.models.campaign_copy import CampaignCopyRequest

    env = await setup(dorg)
    first, _ = await request(env, body(env))
    async with tenant_session(env.tenant_id) as db:  # simulate a run still drafting
        row = await db.get(CampaignCopyRequest, first.id)
        row.status = "generating"
    with pytest.raises(ConflictError) as exc:
        await request(env, body(env))
    assert exc.value.code == "copy_request_open"


async def test_invalid_result_fails_with_fallback_message(dorg: DOrg) -> None:
    env = await setup(dorg)
    bad = [FakeTurn("emit_result", result={"segments": [{"key": "seg_0", "variants": [{"subject": "x", "unexpected": 1}]}]})] * 3
    out, _ = await request(env, body(env), bad)
    row = await request_row(env, out.id)
    assert row.status == "failed"
    got = await detail(env, out.id)
    assert got.fallback_message == FALLBACK_MESSAGE


async def test_budget_exhausted_is_409(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.campaign_copy import requests as req_mod

    env = await setup(dorg)

    async def spent(db: object) -> int:
        return 500_000

    monkeypatch.setattr(req_mod, "agent_tokens_this_month", spent)
    with pytest.raises(ConflictError) as exc:
        await request(env, body(env))
    assert exc.value.code == "budget_exhausted"


@pytest.mark.parametrize("kind", ["api_key", "app", "agent", "workflow", "system", "ai"])
async def test_automated_actors_cannot_request(dorg: DOrg, kind: str) -> None:
    env = await setup(dorg)
    perms = {"campaign:read": Scope.TENANT, "campaign:write": Scope.TENANT}
    if kind in ("api_key", "app"):
        ctx = RequestContext(tenant_id=env.tenant_id, principal=Principal(type=kind, id=uuid.uuid4(), tenant_id=env.tenant_id, permissions=perms))  # type: ignore[arg-type]
    else:
        ctx = RequestContext(tenant_id=env.tenant_id, actor_kind=kind, principal=Principal(type="user", id=env.m.user_id, tenant_id=env.tenant_id, permissions=perms))
    with pytest.raises(Forbidden) as exc:
        await request(env, body(env), ctx=ctx)
    assert exc.value.code == "human_only_action"


async def test_api_route_requires_campaign_read_and_returns_202(dorg: DOrg) -> None:
    from app.services.ai.fake_llm import fake_llm

    env = await setup(dorg)
    data = body(env)
    with fake_llm(script(data["segments"], 2)):
        resp = await env.m.client.post("/api/v1/campaign-copy/requests", json=data, headers={**h(env.m), "Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 202, resp.text
    got = await env.m.client.get(f"/api/v1/campaign-copy/requests/{resp.json()['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["summary"]["variants"] == 4


async def test_no_recipient_data_reaches_the_model(dorg: DOrg) -> None:
    """78.1 AC: every gateway payload of a run is free of the seeded audience's ids, names, emails and phones."""
    env = await setup(dorg)
    out, fake = await request(env, body(env))
    assert (await request_row(env, out.id)).status == "generated"
    payload = json.dumps(fake.calls, default=str).lower()
    assert payload, "the run called the model"
    forbidden = [str(c).lower() for c in env.candidate_ids]
    for name, email, phone in SEEDED:
        forbidden += [name.lower(), *name.lower().split(), email.lower(), phone, phone[-10:]]
    leaked = [f for f in forbidden if re.search(rf"(?<![\w]){re.escape(f)}(?![\w])", payload)]
    assert leaked == [], leaked
