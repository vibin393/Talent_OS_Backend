# ruff: noqa: E501
"""Epic 65 story 65.2 (SM-C14): Proposal review signals -- opened then approved within 5 s -> rubber stamp; approved
without opening -> rubber stamp; rejected -> not; ``opened`` is idempotent per (proposal, user) and human only."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.database import tenant_session
from app.models.proposal_signals import ProposalReviewSignal
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg
from tests.agents.test_executor import start

API = "/api/v1"


async def _proposal(org: AgentOrg) -> str:
    cid = str(org.candidate_id)
    script = [
        FakeTurn("tool_calls", calls=(("messaging.send", {
            "recipient_kind": "candidate", "recipient_id": cid, "channel": "email", "subject": "Hi",
            "body": f"Hello {uuid.uuid4().hex[:6]}",
        }),)),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]  # fmt: skip
    with fake_llm(script):
        ref = await start(org)
    async with tenant_session(org.tenant_id) as db:
        from app.models.agent_runtime import AgentProposal

        return str(
            (
                await db.execute(select(AgentProposal.id).where(AgentProposal.run_id == ref.run_id))
            ).scalar_one()
        )


async def _signal(org: AgentOrg, pid: str) -> ProposalReviewSignal | None:
    async with tenant_session(org.tenant_id) as db:
        row = (
            await db.execute(
                select(ProposalReviewSignal).where(ProposalReviewSignal.proposal_id == uuid.UUID(pid))
            )
        ).scalar_one_or_none()
        if row is not None:
            db.expunge(row)
        return row


def _idem(h: dict[str, str]) -> dict[str, str]:
    return {**h, "Idempotency-Key": str(uuid.uuid4())}


async def test_signals(agent_org: AgentOrg) -> None:
    c, h = agent_org.admin.client, agent_org.admin.h()
    # opened -> approved quickly -> rubber stamp; opened twice keeps first_opened_at
    p1 = await _proposal(agent_org)
    assert (await c.post(f"{API}/ai-governance/proposals/{p1}/opened", headers=h)).status_code == 204
    first = await _signal(agent_org, p1)
    assert first is not None and first.open_count == 1
    assert (await c.post(f"{API}/ai-governance/proposals/{p1}/opened", headers=h)).status_code == 204
    again = await _signal(agent_org, p1)
    assert again is not None and again.open_count == 2 and again.first_opened_at == first.first_opened_at
    resp = await c.post(f"{API}/proposals/{p1}/decide", json={"decision": "approve"}, headers=_idem(h))
    assert resp.status_code == 200, resp.text
    s1 = await _signal(agent_org, p1)
    assert s1 is not None and s1.decision in ("approved", "edited_approved") and s1.rubber_stamp is True

    # never opened -> approved -> rubber stamp
    p2 = await _proposal(agent_org)
    assert (
        await c.post(f"{API}/proposals/{p2}/decide", json={"decision": "approve"}, headers=_idem(h))
    ).status_code == 200
    s2 = await _signal(agent_org, p2)
    assert s2 is not None and s2.first_opened_at is None and s2.rubber_stamp is True

    # opened long ago -> approved -> not a rubber stamp
    p3 = await _proposal(agent_org)
    await c.post(f"{API}/ai-governance/proposals/{p3}/opened", headers=h)
    async with tenant_session(agent_org.tenant_id) as db:
        await db.execute(update(ProposalReviewSignal).where(ProposalReviewSignal.proposal_id == uuid.UUID(p3))
                         .values(first_opened_at=datetime.now(UTC) - timedelta(minutes=2)))  # fmt: skip
    await c.post(f"{API}/proposals/{p3}/decide", json={"decision": "approve"}, headers=_idem(h))
    s3 = await _signal(agent_org, p3)
    assert s3 is not None and s3.rubber_stamp is False and (s3.decision_ms or 0) >= 100_000

    # rejected -> never a rubber stamp
    p4 = await _proposal(agent_org)
    await c.post(
        f"{API}/proposals/{p4}/decide", json={"decision": "reject", "note": "not now"}, headers=_idem(h)
    )
    s4 = await _signal(agent_org, p4)
    assert s4 is not None and s4.decision == "rejected" and s4.rubber_stamp is False


async def test_opened_cross_tenant_and_unknown(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    pid = await _proposal(agent_org)
    oc = other_org.admin.client
    assert (
        await oc.post(f"{API}/ai-governance/proposals/{pid}/opened", headers=other_org.admin.h())
    ).status_code == 404
    c = agent_org.admin.client
    assert (
        await c.post(f"{API}/ai-governance/proposals/{uuid.uuid4()}/opened", headers=agent_org.admin.h())
    ).status_code == 404
