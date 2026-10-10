"""Story 12.4 (FR-62..FR-64): feature toggles + ``require_ai_feature``, 80/100/120 % budget, alerts, usage,
AI action log and CSV export. Matrix rows 14-16, 35."""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.permissions import Principal
from app.dependencies.ai import require_ai_feature
from app.models.ai import AIAction, AIBudgetAlert
from app.models.identity import PlatformOperator
from app.schemas.candidates import ResumeParseOutput
from app.services import ai
from app.services import auth_service as identity
from tests.epic3 import FakeAI, Org3, h, member
from tests.modules.identity.conftest import PASSWORD, new_client

AI = "/api/v1/ai"


@pytest.fixture(autouse=True)
def _fresh_budget_cache() -> None:
    ai.budget.clear_cache()


async def _usage(org: Org3, tokens: int, **extra: Any) -> None:
    async with tenant_session(org.tenant_id) as db:
        db.add(
            AIAction(
                tenant_id=org.tenant_id,
                feature=extra.pop("feature", "resume_parsing"),
                prompt_id="resume_parse",
                initiator_type="system",
                status="ok",
                input_tokens=tokens,
                output_tokens=0,
                **extra,
            )
        )


async def _budget(org: Org3, budget: int | None) -> None:
    async with tenant_session(org.tenant_id) as db:
        await ai.set_monthly_budget(db, org.tenant_id, budget)
    ai.budget.clear_cache()


async def _run(org: Org3, *, bulk: bool) -> ai.AIResult:
    ctx = RequestContext(
        tenant_id=org.tenant_id, principal=Principal(type="user", id=org.admin.user_id, tenant_id=org.tenant_id)
    )
    async with tenant_session(org.tenant_id) as db:
        return await ai.run(
            ctx, db, "resume_parsing", "resume_parse", {}, {"document": "Priya"}, ResumeParseOutput, bulk=bulk
        )


async def _alerts(org: Org3) -> list[int]:
    async with tenant_session(org.tenant_id) as db:
        return sorted((await db.execute(select(AIBudgetAlert.threshold))).scalars())


async def test_budget_100_pauses_bulk_only(org3: Org3, fake_ai: FakeAI) -> None:
    """Matrix 15: budget 1000, usage 1000."""
    await _budget(org3, 1000)
    await _usage(org3, 1000)
    fake_ai.input_tokens, fake_ai.output_tokens = 1, 0
    bulk = await _run(org3, bulk=True)
    assert bulk.error_code == "budget_exceeded"
    interactive = await _run(org3, bulk=False)
    assert interactive.ok
    await _run(org3, bulk=True)
    assert await _alerts(org3) == [80, 100]  # one alert per threshold per month
    async with tenant_session(org3.tenant_id) as db:
        assert await ai.bulk_blocked(db, org3.tenant_id)
    usage = (await org3.admin.client.get(f"{AI}/usage")).json()
    assert (usage["budget"], usage["bulk_paused"], usage["all_blocked"]) == (1000, True, False)
    assert usage["by_feature"]["resume_parsing"] >= 1000


async def test_budget_120_refuses_everything(org3: Org3, fake_ai: FakeAI) -> None:
    """Matrix 16."""
    await _budget(org3, 1000)
    await _usage(org3, 1200)
    assert (await _run(org3, bulk=False)).error_code == "budget_exceeded"
    assert fake_ai.calls == []
    assert await _alerts(org3) == [80, 100, 120]


async def test_operator_sets_budget_tenant_reads_it(app: FastAPI, org3: Org3) -> None:
    email = f"ops-{uuid.uuid4().hex[:8]}@example.com"
    op = await identity.create_operator(email, "Ops", PASSWORD)
    async with new_client(app) as client:
        await client.post("/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD})
        async with platform_session() as db:
            await db.execute(update(PlatformOperator).where(PlatformOperator.id == op.id).values(mfa_enabled=True))
        headers = {"X-CSRF-Token": client.cookies.get("tos_csrf") or ""}
        resp = await client.put(
            f"/api/v1/platform/tenants/{org3.tenant_id}/ai-budget", json={"monthly_token_budget": 5000}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ai_budget"] == 5000
        missing = await client.put(
            f"/api/v1/platform/tenants/{uuid.uuid4()}/ai-budget", json={"monthly_token_budget": 1}, headers=headers
        )
        assert missing.status_code == 404
    settings = (await org3.admin.client.get(f"{AI}/settings")).json()
    assert settings["monthly_token_budget"] == 5000
    denied = await org3.admin.client.put(f"{AI}/settings", json={"monthly_token_budget": 1}, headers=h(org3.admin))
    assert denied.status_code == 403


async def test_feature_toggles_and_dependency(app: FastAPI, org3: Org3) -> None:
    """Matrix 14: ``matching`` disabled -> 403 ``ai_feature_disabled``."""
    viewer = await member(app, org3, "viewer")
    try:
        assert (await viewer.client.get(f"{AI}/features")).json()["matching"] is True
        assert (await viewer.client.put(f"{AI}/features", json={"matching": False}, headers=h(viewer))).status_code == 403
    finally:
        await viewer.client.aclose()
    resp = await org3.admin.client.put(f"{AI}/features", json={"matching": False}, headers=h(org3.admin))
    assert resp.json()["matching"] is False
    unknown = await org3.admin.client.put(f"{AI}/features", json={"telepathy": True}, headers=h(org3.admin))
    assert unknown.status_code == 422

    from app.main import create_app

    test_app = create_app()

    @test_app.get("/api/v1/_test/match", dependencies=[Depends(require_ai_feature("matching"))])
    async def _match() -> dict[str, bool]:
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://testserver") as client:
        client.cookies = org3.admin.client.cookies
        blocked = await client.get("/api/v1/_test/match")
        assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "ai_feature_disabled")
        await org3.admin.client.put(f"{AI}/features", json={"matching": True}, headers=h(org3.admin))
        assert (await client.get("/api/v1/_test/match")).status_code == 200
    # The gateway refuses a disabled feature as a second layer.
    await org3.admin.client.put(f"{AI}/features", json={"resume_parsing": False}, headers=h(org3.admin))
    assert (await _run(org3, bulk=False)).error_code == "feature_disabled"


async def test_ai_action_log_filters_detail_erased_and_csv(app: FastAPI, org3: Org3, other3: Org3) -> None:
    await _usage(org3, 10, feature="jd_generation", output={"text": "JD"})
    await _usage(org3, 20, feature="resume_parsing", output={"full_name": "X"}, input_ref="erased")
    listed = (await org3.admin.client.get(f"{AI}/actions", params={"feature": "jd_generation"})).json()
    assert listed["total"] == 1
    (row,) = listed["items"]
    assert row["output"] == {"text": "JD"}
    erased = (await org3.admin.client.get(f"{AI}/actions", params={"feature": "resume_parsing"})).json()["items"][0]
    assert erased["output"] == "[erased]"
    detail = await org3.admin.client.get(f"{AI}/actions/{row['id']}")
    assert detail.json()["feature"] == "jd_generation"
    assert (await other3.admin.client.get(f"{AI}/actions/{row['id']}")).status_code == 404
    csv_resp = await org3.admin.client.get(f"{AI}/actions/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["X-Export-Rows"] == "2"
    assert csv_resp.text.splitlines()[0].startswith("id,created_at,feature")
    audit_rows = (await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "ai.actions_exported"})).json()
    assert audit_rows["total"] == 1
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(f"{AI}/actions")).status_code == 403
    finally:
        await recruiter.client.aclose()


def test_require_ai_feature_rejects_unknown_feature() -> None:
    with pytest.raises(ValueError, match="unknown AI feature"):
        require_ai_feature("telepathy")
