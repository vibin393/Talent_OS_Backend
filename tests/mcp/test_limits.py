# ruff: noqa: E501
"""Epic 73 (spec 0.6, FR-379/380): per-session-minute, per-user-day and proposals-per-hour limits; the bulk-read
detector pauses the session, holds the user and notifies; release by an administrator and auto-release."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.mcp import McpAccessHold, McpSession
from app.models.notifications import Notification
from app.schemas.mcp import McpSettingsIn
from app.services import mcp_service
from app.services.mcp_server import auth as mcp_auth
from app.services.mcp_server import limits
from tests.mcp.conftest import McpEnv


async def _settings(env: McpEnv, **kw: object) -> None:
    async with tenant_session(env.org.tenant_id) as db:
        await mcp_service.update_settings(env.human_ctx(), db, McpSettingsIn(**kw))  # type: ignore[arg-type]
    mcp_auth.gate_cache.clear()


async def test_calls_per_minute(mcp_env: McpEnv) -> None:
    await _settings(mcp_env, calls_per_minute=5)
    c = await mcp_env.client()
    args = {"action": "other", "entity_type": "candidate", "entity_id": str(mcp_env.org.candidate_id)}
    for _ in range(5):
        await c.call("navigation__screen_for", args)
    out = await c.call("navigation__screen_for", args)
    assert out["isError"] is True and out["content"][0]["text"].startswith("Rate limit reached for this session.")
    assert out["_meta"]["talentos/retry_after"] >= 1


async def test_calls_per_user_day_resets_at_utc_midnight() -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    day = datetime(2026, 10, 10, 23, 59, tzinfo=UTC)
    assert await limits.check_day(tenant, user, 2, now=day) is None
    assert await limits.check_day(tenant, user, 2, now=day) is None
    refused = await limits.check_day(tenant, user, 2, now=day)
    assert refused is not None and "resets at 00:00 UTC" in refused.text
    assert await limits.check_day(tenant, user, 2, now=day + timedelta(minutes=2)) is None


async def test_proposals_per_hour(mcp_env: McpEnv) -> None:
    await _settings(mcp_env, proposals_per_hour=1)
    c = await mcp_env.client()
    args = {"entity_type": "candidate", "entity_id": str(mcp_env.org.candidate_id), "body": "one"}
    first = await c.call("notes__add", args)
    assert first["structuredContent"]["status"] == "proposed", first
    second = await c.call("notes__add", {**args, "body": "two"})
    assert second["isError"] is True and "created 1 proposals in the last hour" in second["content"][0]["text"]


async def test_bulk_read_detector_pauses_holds_and_notifies(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    key = hashlib.sha256(c.session_id.encode()).hexdigest()  # type: ignore[union-attr]
    await limits.add_candidates(key, [uuid.uuid4() for _ in range(500)])
    out = await c.call("candidates__get", {"candidate_id": str(mcp_env.org.candidate_id)})
    assert out["isError"] is True and "paused" in out["content"][0]["text"]
    async with tenant_session(mcp_env.org.tenant_id) as db:
        row = (await db.execute(select(McpSession))).scalar_one()
        assert (row.status, row.end_reason) == ("paused", "bulk_read") and row.distinct_candidates == 501
        hold = (await db.execute(select(McpAccessHold))).scalar_one()
        assert hold.reason == "bulk_read" and hold.auto_release_at is not None
        notes = (await db.execute(select(Notification).where(Notification.type == "mcp_session_paused"))).scalars().all()
        assert notes
    assert (await c.rpc("ping")).status_code in (403, 404)  # the hold gate answers first
    mcp_auth.gate_cache.clear()
    blocked = await mcp_env.client(init=False)
    assert (await blocked.initialize(expect_ok=False)).status_code == 403
    async with tenant_session(mcp_env.org.tenant_id) as db:
        holds = await mcp_service.list_holds(mcp_env.human_ctx(), db)
        assert len(holds) == 1
        await mcp_service.release_hold(mcp_env.human_ctx(), db, holds[0].id, note="Reviewed")
    again = await mcp_env.client()
    assert await again.result("ping") == {}


async def test_auto_release(mcp_env: McpEnv) -> None:
    from app.services.mcp_store import holds

    org = mcp_env.org
    async with tenant_session(org.tenant_id) as db:
        hold = await holds.open_(db, tenant_id=org.tenant_id, user_id=org.admin.user_id, installation_id=None, session_id=None, reason="bulk_read", auto_release_hours=24)
        assert hold is not None
        hold.auto_release_at = datetime.now(UTC) - timedelta(minutes=1)
    async with tenant_session(org.tenant_id) as db:
        assert await holds.active_for(db, org.admin.user_id) is None
    result = await mcp_service.rollup(org.tenant_id)
    assert result["released"] == 1
