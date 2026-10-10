# ruff: noqa: E501
"""Epic 53 Proposals (53.5): routing, approve as the approver (audit via_proposal_id), edit-and-approve, reject,
staleness, ai_actions write-back, human-only deciders, Copilot confirm / discard, expiry."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.audit import AuditLog
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.permissions import Principal
from app.models.agent_runtime import AgentProposal
from app.models.ai import AIAction
from app.schemas.agentic import ProposalDecisionIn
from app.services import agent_runtime_service as svc
from app.services.agent_runtime import maintenance
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg
from tests.agents.test_executor import start
from tests.modules.identity.conftest import join


async def _proposal(org: AgentOrg, body: str = "Hello Priya") -> AgentProposal:
    cid = str(org.candidate_id)
    script = [
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "messaging.send",
                    {
                        "recipient_kind": "candidate",
                        "recipient_id": cid,
                        "channel": "email",
                        "subject": "Hi",
                        "body": body,
                        "_rationale": "follow up",
                    },
                ),
            ),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]
    with fake_llm(script):
        ref = await start(org)
    async with tenant_session(org.tenant_id) as db:
        p = (await db.execute(select(AgentProposal).where(AgentProposal.run_id == ref.run_id))).scalar_one()
        db.expunge(p)
    return p


async def _live_ctx(org: AgentOrg, user_id: uuid.UUID) -> RequestContext:
    from app.services import entity_refs

    async with tenant_session(org.tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, org.tenant_id, user_id)
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def test_reject_needs_note_and_writes_back(agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    assert p.rationale == "follow up" and p.status == "pending"
    ctx = await _live_ctx(agent_org, agent_org.admin.user_id)
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(ValidationFailed):
            await svc.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="reject"))
    async with tenant_session(agent_org.tenant_id) as db:
        out = await svc.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="reject", note="not now"))
    assert out.status == "rejected"
    async with tenant_session(agent_org.tenant_id) as db:
        decisions = {
            a.decision
            for a in (await db.execute(select(AIAction).where(AIAction.agent_run_id == p.run_id))).scalars()
        }
    assert decisions == {"rejected"}


async def test_approve_executes_as_approver_with_audit_link(agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    ctx = await _live_ctx(agent_org, agent_org.admin.user_id)
    async with tenant_session(agent_org.tenant_id) as db:
        out = await svc.decide_proposal(
            ctx,
            db,
            p.id,
            ProposalDecisionIn(decision="approve", edited_args={**p.args, "body": "Hello Priya, edited"}),
        )
    assert out.status == "executed", out.error_code
    assert out.edited is True
    async with tenant_session(agent_org.tenant_id) as db:
        rows = list((await db.execute(select(AuditLog).where(AuditLog.via_proposal_id == p.id))).scalars())
        decisions = {
            a.decision
            for a in (await db.execute(select(AIAction).where(AIAction.agent_run_id == p.run_id))).scalars()
        }
    assert rows and all(r.actor_type == "user" and r.actor_id == agent_org.admin.user_id for r in rows)
    assert decisions == {"edited"}


async def test_edit_raising_risk_or_invalid_is_refused(agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    ctx = await _live_ctx(agent_org, agent_org.admin.user_id)
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await svc.decide_proposal(
                ctx,
                db,
                p.id,
                ProposalDecisionIn(decision="approve", edited_args={**p.args, "channel": "fax"}),
            )
    assert exc.value.code == "proposal_edit_not_allowed"


async def test_non_human_and_unrelated_deciders(app: FastAPI, agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    agent_ctx = RequestContext(
        tenant_id=agent_org.tenant_id,
        principal=Principal(type="agent", id=uuid.uuid4(), tenant_id=agent_org.tenant_id),
        actor_kind="agent",
        actor_id=uuid.uuid4(),
    )
    api_ctx = RequestContext(
        tenant_id=agent_org.tenant_id,
        principal=Principal(type="api_key", id=uuid.uuid4(), tenant_id=agent_org.tenant_id),
    )
    for ctx in (
        agent_ctx,
        api_ctx,
        RequestContext(tenant_id=agent_org.tenant_id, actor_kind="workflow", actor_id=uuid.uuid4()),
    ):
        async with tenant_session(agent_org.tenant_id) as db:
            with pytest.raises(Forbidden) as exc:
                await svc.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="reject", note="nope"))
        assert exc.value.code == "human_only_action"
    recruiter = await join(app, agent_org.tenant, "recruiter", name="Rita Recruiter")
    try:
        ctx = await _live_ctx(agent_org, recruiter.user_id)
        async with tenant_session(agent_org.tenant_id) as db:
            with pytest.raises(NotFound):
                await svc.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="reject", note="nope"))
    finally:
        await recruiter.client.aclose()


async def test_copilot_draft_confirm_only_by_owner(app: FastAPI, agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    async with tenant_session(agent_org.tenant_id) as db:
        await db.execute(update(AgentProposal).where(AgentProposal.id == p.id).values(origin="copilot"))
    ctx = await _live_ctx(agent_org, agent_org.admin.user_id)
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await svc.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="approve"))
    assert exc.value.code == "use_confirm"
    other = await join(app, agent_org.tenant, name="Second Admin")
    try:
        octx = await _live_ctx(agent_org, other.user_id)
        async with tenant_session(agent_org.tenant_id) as db:
            with pytest.raises(NotFound):
                await svc.confirm_draft(octx, db, p.id)
    finally:
        await other.client.aclose()
    async with tenant_session(agent_org.tenant_id) as db:
        out = await svc.discard_draft(ctx, db, p.id)
    assert out.status == "discarded"


async def test_expiry_marks_expired(agent_org: AgentOrg) -> None:
    p = await _proposal(agent_org)
    async with tenant_session(agent_org.tenant_id) as db:
        await db.execute(
            update(AgentProposal)
            .where(AgentProposal.id == p.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    async with tenant_session(agent_org.tenant_id) as db:
        result = await maintenance.expire_proposals(db)
    assert result["expired"] == 1
    async with tenant_session(agent_org.tenant_id) as db:
        assert (await db.get(AgentProposal, p.id)).status == "expired"  # type: ignore[union-attr]
