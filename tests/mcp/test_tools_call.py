# ruff: noqa: E501
"""Epic 73 (spec 0.4, FR-377/378): ``tools/call`` through the runtime -- reads execute; low internal tools are Proposals
unless the tenant opted in; ``mcp:propose`` tools and high tools are always Proposals; the result mapping, dedup on a
retried JSON-RPC id, redaction, the unknown-tool path (logged on the run) and exposure records."""

import pytest
from sqlalchemy import select

from app.core.agentic.exposure import get_exposure
from app.core.agentic.tools import tools
from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.agent_runtime import AgentProposal, AgentToolCall
from app.models.mcp import McpSession
from app.schemas.mcp import McpSettingsIn
from app.services import mcp_service
from app.services.agent_runtime.policy import PolicyInput, resolve
from app.services.mcp_server import auth as mcp_auth
from tests.mcp.conftest import McpEnv


async def _session(env: McpEnv) -> McpSession:
    async with tenant_session(env.org.tenant_id) as db:
        return (await db.execute(select(McpSession).order_by(McpSession.started_at.desc()))).scalars().first()  # type: ignore[return-value]


async def test_read_executes_and_records_exposure(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    out = await c.call("candidates__get", {"candidate_id": str(mcp_env.org.candidate_id)})
    assert out["isError"] is False, out
    assert out["structuredContent"]["id"] == str(mcp_env.org.candidate_id) or "candidate" in str(out["structuredContent"])
    assert out["_meta"]["talentos/truncated"] is False and "talentos/untrusted" in out["_meta"]
    assert "priya.raman@example.com" not in out["content"][0]["text"]
    row = await _session(mcp_env)
    assert row.call_count == 1 and row.distinct_candidates >= 1
    from app.models.ecosystem import SubjectExposure
    from app.services import exposure_service

    await exposure_service.flush()
    async with tenant_session(mcp_env.org.tenant_id) as db:
        exp = (await db.execute(select(SubjectExposure).where(SubjectExposure.channel == "mcp"))).scalars().all()
        assert any(e.subject_id == mcp_env.org.candidate_id and e.installation_id == mcp_env.installation.id for e in exp)


async def test_low_tool_is_a_proposal_then_auto_with_opt_in(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    args = {"entity_type": "candidate", "entity_id": str(mcp_env.org.candidate_id), "body": "From the assistant"}
    out = await c.call("notes__add", args, request_id=77)
    assert out["isError"] is False, out
    assert out["structuredContent"]["status"] == "proposed" and "Nothing has been done yet." in out["content"][0]["text"]
    again = await c.call("notes__add", args, request_id=77)  # a client retry of the same JSON-RPC id
    assert again["structuredContent"]["proposal_id"] == out["structuredContent"]["proposal_id"]
    async with tenant_session(mcp_env.org.tenant_id) as db:
        proposals = (await db.execute(select(AgentProposal))).scalars().all()
        assert len(proposals) == 1 and "through MCP" in proposals[0].title and proposals[0].status == "pending"
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await mcp_service.update_settings(mcp_env.human_ctx(), db, McpSettingsIn(low_tool_modes={"notes.add": "auto"}))
    done = await c.call("notes__add", {**args, "body": "Second"})
    assert done["isError"] is False and done["structuredContent"].get("status") != "proposed", done


async def test_low_tool_modes_only_for_draft_tools(mcp_env: McpEnv) -> None:
    async with tenant_session(mcp_env.org.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await mcp_service.update_settings(mcp_env.human_ctx(), db, McpSettingsIn(low_tool_modes={"interviews.reschedule": "auto"}))
        assert exc.value.code == "not_configurable"


def test_propose_exposure_never_auto_even_for_a_low_tool() -> None:
    tool = tools()["interviews.reschedule"]
    assert tool.risk == "low"
    decision = resolve(
        PolicyInput(
            global_kill=False, tenant_kill=False, agentic_on=True, agent_on=True, allow_list=frozenset({tool.key}), tool=tool,
            args=None, effective_permissions=dict.fromkeys(tool.permissions, "tenant"), action_policy_mode="auto",
            config_default_mode=None, spec_default_mode="suggest", origin="external", run_tainted=True, suspicious=False,
            burn_in_until=None, now=None, exposure=get_exposure(tool.key), token_scopes=frozenset({"mcp:propose"}),
            mcp_groups_enabled=frozenset({"interviews"}),
        )  # type: ignore[arg-type]
    )
    assert decision.mode == "suggest"


async def test_high_send_is_a_proposal(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    await c.call("candidates__get", {"candidate_id": str(mcp_env.org.candidate_id)})
    out = await c.call(
        "messaging__send",
        {"recipient_kind": "candidate", "recipient_id": str(mcp_env.org.candidate_id), "channel": "email", "subject": "Hi", "body": "Hello"},
    )
    assert out["structuredContent"]["status"] == "proposed", out
    assert out["structuredContent"]["proposal_url"].endswith(out["structuredContent"]["proposal_id"])
    row = await _session(mcp_env)
    assert row.proposal_count == 1


async def test_unknown_and_disabled_group_tools(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    resp = await c.rpc("tools/call", {"name": "offers__release", "arguments": {}})
    assert resp.json()["error"]["code"] == -32602 and resp.json()["error"]["message"] == "Unknown tool"
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await mcp_service.update_settings(mcp_env.human_ctx(), db, McpSettingsIn(tool_groups=["core"]))
    mcp_auth.gate_cache.clear()
    resp = await c.rpc("tools/call", {"name": "candidates__get", "arguments": {"candidate_id": str(mcp_env.org.candidate_id)}})
    assert resp.json()["error"]["code"] == -32602
    row = await _session(mcp_env)
    async with tenant_session(mcp_env.org.tenant_id) as db:
        calls = (await db.execute(select(AgentToolCall).where(AgentToolCall.run_id == row.run_id))).scalars().all()
        assert any(call.outcome in ("policy_violation", "refused") for call in calls)
    assert row.violation_count >= 1


async def test_invalid_and_not_found(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    bad = await c.call("candidates__get", {"candidate_id": "not-a-uuid"})
    assert bad["isError"] is True and bad["content"][0]["text"].startswith("The arguments don't match the tool's schema: candidate_id")
    import uuid

    missing = await c.call("candidates__get", {"candidate_id": str(uuid.uuid4())})
    assert missing["isError"] is True and missing["content"][0]["text"] == "No matching record was found."


async def test_star_request_gets_a_deep_link(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    out = await c.call("navigation__screen_for", {"action": "reject_application", "entity_type": "candidate", "entity_id": str(mcp_env.org.candidate_id)})
    assert out["isError"] is False and "Only a person can do this" in out["content"][0]["text"], out
