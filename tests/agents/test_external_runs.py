# ruff: noqa: E501
"""Epic 66 (spec 0.15, AD-84): external (MCP) sessions go through the same executor path as model tool calls.

A read tool executes (masked, untrusted-wrapped, candidate ids returned for the bulk-read detector, D-25(b)); a low
write tool is only proposed (tenant opt-in absent) and never executed; a tool that is not exposed is refused; a token
without the exposure's scope is refused; a repeated JSON-RPC id is deduplicated; closing ends the session."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.core.agentic.exposure import ToolExposure, expose_tool, get_exposure, unexpose_tool
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal, Scope
from app.models.agent_runtime import AgentProposal, AgentRun
from app.schemas.agentic import ExternalCallIn, ExternalSessionIn
from app.services import agent_runtime_service as svc
from app.services import ecosystem_service
from app.services.agent_runtime.placeholders import refresh_mcp_spec
from tests.agents.conftest import AgentOrg, enable


@pytest.fixture
def exposed() -> Iterator[None]:
    # Epic 73 exposes notes.add / messaging.create_draft itself (groups core / outreach); only add what is missing.
    added = []
    for exp in (
        ToolExposure("consent.status", frozenset({"mcp"}), "mcp:read", ("candidates:read",)),
        ToolExposure("notes.add", frozenset({"mcp"}), "mcp:draft"),
        ToolExposure("messaging.create_draft", frozenset({"mcp"}), "mcp:draft"),
    ):
        if get_exposure(exp.tool_key) is None:
            expose_tool(exp)
            added.append(exp.tool_key)
    refresh_mcp_spec()
    yield
    for key in added:
        unexpose_tool(key)
    refresh_mcp_spec()


def _app_ctx(org: AgentOrg, inst: uuid.UUID, scopes: set[str]) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type="app",
            id=inst,
            tenant_id=org.tenant_id,
            permissions={"candidate:read": Scope.TENANT, "note:write": Scope.TENANT},
            app_installation_id=inst,
            acting_user_id=org.admin.user_id,
            oauth_scopes=frozenset(scopes),
            resource="mcp",
        ),
    )


async def _enable_mcp(org: AgentOrg) -> None:
    await enable(org.tenant_id, agent_mcp=True)
    human = RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type="user", id=org.admin.user_id, tenant_id=org.tenant_id),
    )
    async with tenant_session(org.tenant_id) as db:
        await ecosystem_service.set_mcp_enabled(human, db, on=True, notice_version="v1", tool_groups=["core", "outreach"])


async def _open(org: AgentOrg, ctx: RequestContext, inst: uuid.UUID) -> uuid.UUID:
    async with tenant_session(org.tenant_id) as db:
        return await svc.open_external_session(
            ctx,
            db,
            ExternalSessionIn(
                installation_id=inst,
                user_id=org.admin.user_id,
                token_id=uuid.uuid4(),
                client_name="Claude\n(desktop)",
            ),
        )


async def _call(
    org: AgentOrg, ctx: RequestContext, run_id: uuid.UUID, tool: str, args: dict, call_id: str
) -> svc.ExternalCallOut:  # type: ignore[type-arg]
    async with tenant_session(org.tenant_id) as db:
        return await svc.execute_external_call(
            ctx, db, ExternalCallIn(run_id=run_id, tool_key=tool, args=args, call_id=call_id)
        )


async def test_external_session_paths(agent_org: AgentOrg, exposed: None) -> None:
    await _enable_mcp(agent_org)
    inst = uuid.uuid4()
    ctx = _app_ctx(agent_org, inst, {"mcp:read", "mcp:draft", "candidates:read"})
    run_id = await _open(agent_org, ctx, inst)
    async with tenant_session(agent_org.tenant_id) as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        assert (run.origin, run.trigger_kind, run.status, run.tainted) == (
            "external",
            "external",
            "running",
            True,
        )
        assert run.external_client == "Claude (desktop)" and run.app_installation_id == inst

    read = await _call(
        agent_org, ctx, run_id, "consent.status", {"candidate_id": str(agent_org.candidate_id)}, "1"
    )
    assert read.outcome == "executed", read
    again = await _call(
        agent_org, ctx, run_id, "consent.status", {"candidate_id": str(agent_org.candidate_id)}, "1"
    )
    assert again.outcome == "deduplicated"

    # notes.add is exposed, but no granted scope unlocks note:write: delegated rights never exceed the token.
    note = await _call(
        agent_org, ctx, run_id, "notes.add",
        {"entity_type": "candidate", "entity_id": str(agent_org.candidate_id), "body": "From MCP"}, "2",
    )  # fmt: skip
    assert note.outcome == "refused", note
    draft = await _call(
        agent_org, ctx, run_id, "messaging.create_draft",
        {"recipient_kind": "candidate", "recipient_id": str(agent_org.candidate_id), "channel": "email", "body": "Hello"},
        "2b",
    )  # fmt: skip
    assert draft.outcome == "proposed" and draft.proposal_id is not None and draft.proposal_url, draft
    async with tenant_session(agent_org.tenant_id) as db:
        proposal = await db.get(AgentProposal, draft.proposal_id)
        assert proposal is not None and proposal.status == "pending" and proposal.app_installation_id == inst
        assert proposal.origin == "external" and "through MCP" in proposal.title

    hidden = await _call(
        agent_org,
        ctx,
        run_id,
        "tasks.create",
        {"title": "x", "entity_type": "candidate", "entity_id": str(agent_org.candidate_id)},
        "3",
    )
    assert hidden.outcome in ("refused", "invalid", "not_found")

    async with tenant_session(agent_org.tenant_id) as db:
        await svc.close_external_session(ctx, db, run_id, reason="completed")
        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        assert run.status in ("completed", "stopped")
    closed = await _call(
        agent_org, ctx, run_id, "consent.status", {"candidate_id": str(agent_org.candidate_id)}, "4"
    )
    assert closed.outcome == "refused"


async def test_insufficient_scope_is_refused(agent_org: AgentOrg, exposed: None) -> None:
    await _enable_mcp(agent_org)
    inst = uuid.uuid4()
    ctx = _app_ctx(agent_org, inst, {"mcp:read"})  # lacks candidates:read
    run_id = await _open(agent_org, ctx, inst)
    out = await _call(
        agent_org, ctx, run_id, "consent.status", {"candidate_id": str(agent_org.candidate_id)}, "1"
    )
    assert out.outcome == "refused"
