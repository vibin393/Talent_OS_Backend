# ruff: noqa: E501
"""Epic 78 story 78.3 (FR-403, AD-43): apply modes (single, A/B subjects, a campaign per segment), only approved copy,
only draft targets; the P3 guards -- submit after a post-approval edit -> 409 ``copy_reapproval_required``, campaigns
without AI copy unaffected, the P3 approval threshold chain still starts; the sequence activation guard."""

import uuid
from typing import Any

import pytest

from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.models.campaigns import Campaign
from app.schemas.campaign_copy import ApplyIn, ApproveIn
from tests.campaign_copy.helpers import (
    GOOD_EMAIL,
    Env,
    body,
    call,
    drafts,
    opened_long_ago,
    request,
    script,
    setup,
)
from tests.epic5 import h
from tests.modules.campaigns.helpers import API, setup_marketing, submit
from tests.modules.demands.helpers import DOrg


async def _approved(env: Env, data: dict[str, Any], variants: list[dict[str, Any]] | None = None, approve: int = 1) -> list[Any]:
    n = data["variants_per_segment"]
    out, _ = await request(env, data, script(data["segments"], n, per_variant=variants or [GOOD_EMAIL] * n))
    rows = await drafts(env, out.id)
    for d in rows[:approve]:
        await opened_long_ago(env, d.id)
        await call(env, "approve", d.id, ApproveIn())
    return rows


def _one_segment(env: Env, variants: int = 1) -> dict[str, Any]:
    return body(env, segments=[{"key": "pune", "label": "Pune engineers", "source": {"kind": "attribute", "field": "city", "value": "Pune"}}], variants_per_segment=variants)


async def _campaign(env: Env) -> Campaign:
    async with tenant_session(env.tenant_id) as db:
        c = await db.get(Campaign, env.campaign_id)
        assert c is not None
        db.expunge(c)
    return c


async def test_single_apply_sets_copy_and_marks_used(dorg: DOrg) -> None:
    env = await setup(dorg)
    (d,) = await _approved(env, _one_segment(env))
    out = await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    assert out.campaign_ids == [env.campaign_id]
    c = await _campaign(env)
    assert c.email_subject == GOOD_EMAIL["subject"] and c.email_body_html == GOOD_EMAIL["body_html"]
    (row,) = await drafts(env, d.request_id)
    assert row.status == "used" and row.used_in_campaign_id == env.campaign_id
    status = await call(env, "campaign_status", env.campaign_id)
    assert status.uses_ai_copy and not status.changed_after_approval


async def test_unapproved_copy_cannot_be_applied(dorg: DOrg) -> None:
    env = await setup(dorg)
    (d,) = await _approved(env, _one_segment(env), approve=0)
    with pytest.raises(ConflictError) as exc:
        await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    assert exc.value.code == "copy_not_approved"


async def test_non_draft_campaign_is_409(dorg: DOrg) -> None:
    env = await setup(dorg)
    (d,) = await _approved(env, _one_segment(env))
    resp = await env.m.client.post(f"{API}/campaigns/{env.campaign_id}/cancel", json={"reason": "stop"}, headers=h(env.m))
    assert resp.status_code == 200, resp.text
    with pytest.raises(ConflictError) as exc:
        await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    assert exc.value.code == "campaign_not_draft"


async def test_ab_subjects_two_variants_only(dorg: DOrg) -> None:
    env = await setup(dorg)
    variants = [{**GOOD_EMAIL, "subject": s} for s in ("Data roles for you", "New openings this week", "Your next data role")]
    rows = await _approved(env, _one_segment(env, 3), variants, approve=3)
    with pytest.raises(ValidationFailed) as exc:
        await call(env, "apply", ApplyIn(draft_ids=[r.id for r in rows], mode="ab_subjects"))
    assert exc.value.code == "ab_two_variants_only"
    await call(env, "apply", ApplyIn(draft_ids=[rows[0].id, rows[1].id], mode="ab_subjects"))
    c = await _campaign(env)
    assert [v["subject"] for v in c.ab_test["variants"]] == ["Data roles for you", "New openings this week"]


async def test_submit_after_post_approval_edit_needs_reapproval(dorg: DOrg) -> None:
    env = await setup(dorg)
    (d,) = await _approved(env, _one_segment(env))
    await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    edit = await env.m.client.patch(f"{API}/campaigns/{env.campaign_id}", json={"email_subject": "Changed by hand"}, headers=h(env.m))
    assert edit.status_code == 200, edit.text
    status = await call(env, "campaign_status", env.campaign_id)
    assert status.changed_after_approval
    resp = await submit(env.m, str(env.campaign_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "copy_reapproval_required"


async def test_approved_copy_goes_through_the_p3_approval_chain(dorg: DOrg) -> None:
    env = await setup(dorg)
    await setup_marketing(env.m, approval_threshold=1)
    (d,) = await _approved(env, _one_segment(env))
    await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    resp = await submit(env.m, str(env.campaign_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_approval"  # threshold reached: the P3 approval chain starts unchanged


async def test_campaign_without_ai_copy_is_unaffected(dorg: DOrg) -> None:
    env = await setup(dorg)
    resp = await submit(env.m, str(env.campaign_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("scheduled", "sending", "completed", "pending_approval")


async def test_campaign_per_segment(dorg: DOrg) -> None:
    env = await setup(dorg)
    m = env.m
    pool = await m.client.post("/api/v1/talent-pools", json={"name": "Pune engineers"}, headers=h(m))
    assert pool.status_code == 201, pool.text
    await m.client.post(f"/api/v1/talent-pools/{pool.json()['id']}/members", json={"candidate_ids": [str(env.candidate_ids[0])]}, headers=h(m))
    aud = await m.client.post(f"{API}/audiences", json={"name": "Pool audience", "sources": [{"mode": "include", "kind": "pool", "talent_pool_id": pool.json()["id"]}]}, headers=h(m))
    assert aud.status_code == 201, aud.text
    source_id = aud.json()["sources"][0]["id"]
    data = body(env, segments=[{"key": "pool", "label": "Pool members", "source": {"kind": "audience_source", "id": source_id}}], variants_per_segment=1)
    (d,) = await _approved(env, data)
    got = await call(env, "get_request", d.request_id)
    assert got.segments[0].reachable is not None  # aggregate count only
    out = await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="per_segment_campaigns"))
    assert len(out.campaign_ids) == 1 and out.campaign_ids[0] != env.campaign_id
    async with tenant_session(env.tenant_id) as db:
        child = await db.get(Campaign, out.campaign_ids[0])
        assert child is not None and child.status == "draft" and child.email_subject == GOOD_EMAIL["subject"]
    base = await _campaign(env)
    assert base.email_subject == "Hello {{candidate.first_name}}"  # the base campaign is unchanged


async def test_sequence_step_copy_and_activation_guard(dorg: DOrg) -> None:
    env = await setup(dorg)
    m = env.m
    step = {"kind": "send", "config": {"channel": "email", "subject": "Hi {{candidate.first_name}}", "body_html": "<p>New roles</p>"}}
    seq = await m.client.post(f"{API}/sequences", json={"name": "Nurture", "steps": [step]}, headers=h(m))
    assert seq.status_code == 201, seq.text
    step_id = seq.json()["steps"][0]["id"]
    data = {**_one_segment(env), "campaign_id": None, "sequence_step_id": step_id}
    (d,) = await _approved(env, data)
    await call(env, "apply", ApplyIn(draft_ids=[d.id], mode="single"))
    pub = await m.client.post(f"{API}/sequences/{seq.json()['id']}/publish", json={"note": "v1"}, headers=h(m))
    assert pub.status_code == 200, pub.text
    assert pub.json()["published_steps"][0]["config"]["subject"] == GOOD_EMAIL["subject"]
    changed = {**step, "config": {**step["config"], "subject": "Changed by hand"}}
    upd = await m.client.patch(f"{API}/sequences/{seq.json()['id']}", json={"steps": [changed]}, headers=h(m))
    assert upd.status_code == 200, upd.text
    again = await m.client.post(f"{API}/sequences/{seq.json()['id']}/publish", json={"note": "v2"}, headers=h(m))
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "copy_reapproval_required"


async def test_unknown_draft_is_422(dorg: DOrg) -> None:
    env = await setup(dorg)
    with pytest.raises(ValidationFailed):
        await call(env, "apply", ApplyIn(draft_ids=[uuid.uuid4()], mode="single"))
