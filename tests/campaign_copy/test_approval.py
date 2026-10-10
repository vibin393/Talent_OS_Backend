# ruff: noqa: E501
"""Epic 78 story 78.3 (FR-403, FR-409): ★ approval of AI copy -- automated actors refused, block findings refuse,
warnings need an acknowledgement, an edit clears the approval, SM-C23 rubber-stamp signal, siblings stay unapproved."""

import uuid

import pytest

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden
from app.core.permissions import Principal, Scope
from app.schemas.campaign_copy import ApproveIn, DraftEditIn
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
from tests.modules.demands.helpers import DOrg


async def _drafts(env: Env, variant: dict | None = None, *, segments: int = 1, variants: int = 2) -> list:
    data = body(env, segments=[{"key": f"s{i}", "label": f"Segment {i}", "source": {"kind": "attribute", "field": "city", "value": "Pune"}} for i in range(segments)], variants_per_segment=variants)
    out, _ = await request(env, data, script(data["segments"], variants, variant))
    rows = await drafts(env, out.id)
    assert len(rows) == segments * variants, rows
    return rows


@pytest.mark.parametrize("kind", ["api_key", "app", "agent", "workflow", "system", "ai"])
async def test_automated_actors_cannot_approve(dorg: DOrg, kind: str) -> None:
    env = await _env(dorg)
    d = (await _drafts(env))[0]
    perms = {"campaign:read": Scope.TENANT, "campaign:copy_approve": Scope.TENANT}
    if kind in ("api_key", "app"):
        ctx = RequestContext(tenant_id=env.tenant_id, principal=Principal(type=kind, id=uuid.uuid4(), tenant_id=env.tenant_id, permissions=perms))  # type: ignore[arg-type]
    else:
        ctx = RequestContext(tenant_id=env.tenant_id, actor_kind=kind, principal=Principal(type="user", id=env.m.user_id, tenant_id=env.tenant_id, permissions=perms))
    with pytest.raises(Forbidden) as exc:
        await call(env, "approve", d.id, ApproveIn(), ctx=ctx)
    assert exc.value.code == "human_only_action"


async def _env(dorg: DOrg) -> Env:
    return await setup(dorg)


async def test_user_without_copy_approve_is_refused(dorg: DOrg) -> None:
    env = await _env(dorg)
    d = (await _drafts(env))[0]
    ctx = RequestContext(tenant_id=env.tenant_id, principal=Principal(type="user", id=env.m.user_id, tenant_id=env.tenant_id, permissions={"campaign:read": Scope.TENANT, "campaign:write": Scope.TENANT}))
    with pytest.raises(Forbidden):
        await call(env, "approve", d.id, ApproveIn(), ctx=ctx)


async def test_block_findings_refuse_approval(dorg: DOrg) -> None:
    env = await _env(dorg)
    bad = {**GOOD_EMAIL, "body_html": "<p>Only male candidates. Earn ₹18 LPA.</p>"}
    d = (await _drafts(env, bad))[0]
    assert {f["code"] for f in d.findings} >= {"protected_attribute", "compensation_out_of_range"}
    with pytest.raises(ConflictError) as exc:
        await call(env, "approve", d.id, ApproveIn())
    assert exc.value.code == "copy_checks_failed"


async def test_warnings_need_acknowledgement(dorg: DOrg) -> None:
    env = await _env(dorg)
    warn = {**GOOD_EMAIL, "body_html": "<p>Hi {{candidate.first_name}}, we need a rockstar data engineer.</p>"}
    d = (await _drafts(env, warn))[0]
    with pytest.raises(ConflictError) as exc:
        await call(env, "approve", d.id, ApproveIn())
    assert exc.value.code == "copy_warnings_unacknowledged"
    acked = await call(env, "acknowledge_warnings", d.id)
    assert acked.warnings_acknowledged
    await opened_long_ago(env, d.id)
    out = await call(env, "approve", d.id, ApproveIn(note="ok"))
    assert out.status == "approved"


async def test_approve_rubber_stamp_and_siblings(dorg: DOrg) -> None:
    env = await _env(dorg)
    a, b = await _drafts(env)
    unopened = await call(env, "approve", a.id, ApproveIn())
    assert unopened.status == "approved" and unopened.rubber_stamp is True and unopened.approved_by == env.m.user_id
    rows = {r.id: r for r in await drafts(env, a.request_id)}
    assert rows[b.id].status == "checked" and rows[b.id].approved_by is None  # siblings are never approved
    await call(env, "mark_opened", b.id)
    quick = await call(env, "approve", b.id, ApproveIn())
    assert quick.rubber_stamp is True  # < 10 s after opening


async def test_considered_approval_is_not_a_rubber_stamp(dorg: DOrg) -> None:
    env = await _env(dorg)
    d = (await _drafts(env))[0]
    await opened_long_ago(env, d.id, 30)
    out = await call(env, "approve", d.id, ApproveIn())
    assert out.rubber_stamp is False
    row = next(r for r in await drafts(env, d.request_id) if r.id == d.id)
    assert row.approval_ms is not None and row.approval_ms >= 10_000 and row.approved_sha256 == row.content_sha256


async def test_edit_after_approval_clears_it(dorg: DOrg) -> None:
    env = await _env(dorg)
    d = (await _drafts(env))[0]
    await opened_long_ago(env, d.id)
    await call(env, "approve", d.id, ApproveIn())
    edited = await call(env, "edit_draft", d.id, DraftEditIn(subject="Fresh data roles this week"))
    assert edited.status == "edited" and edited.approved_by is None and edited.approved_at is None
    again = await call(env, "approve", d.id, ApproveIn())
    assert again.status == "approved"


async def test_approve_route_requires_copy_approve_permission(dorg: DOrg) -> None:
    env = await _env(dorg)
    d = (await _drafts(env))[0]
    resp = await env.m.client.post(f"/api/v1/campaign-copy/drafts/{d.id}/approve", json={}, headers=h(env.m))
    assert resp.status_code == 200, resp.text  # the Tenant Admin holds campaign:copy_approve
    assert resp.json()["status"] == "approved"
