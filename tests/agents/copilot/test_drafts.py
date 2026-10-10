# ruff: noqa: E501
"""Epic 54 Draft Actions (54.3): every Copilot write is a pending ``origin='copilot'`` Proposal assigned to the user;
confirm executes as the user (audit ``via_proposal_id``), discard / expiry / stub tools, event messages."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.agent_runtime import AgentConfig, AgentProposal
from app.services.agent_runtime import proposals as runtime_proposals
from tests.agents.copilot.conftest import C, CopilotOrg, answer, ask, h, messages, new_conversation


def _send(org: CopilotOrg) -> list[dict]:
    cid = str(org.candidate_id)
    return [
        {"kind": "tool_calls", "calls": [["candidates.get", {"candidate_id": cid}]]},
        {
            "kind": "tool_calls",
            "calls": [
                [
                    "messaging.send",
                    {
                        "recipient_kind": "candidate",
                        "recipient_id": cid,
                        "channel": "email",
                        "subject": "A new Java role",
                        "body": "Hi Priya, are you open to a new role?",
                    },
                ]
            ],
        },
        {"kind": "text", "text": "I've prepared a draft for you to review."},
    ]


async def _proposals(org: CopilotOrg, run_id: str) -> list[AgentProposal]:
    async with tenant_session(org.tenant_id) as db:
        rows = list(
            (
                await db.execute(select(AgentProposal).where(AgentProposal.run_id == uuid.UUID(run_id)))
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


async def test_outreach_is_a_draft_and_confirm_sends_as_user(cop: CopilotOrg) -> None:
    conv = await new_conversation(
        cop.admin,
        page_context={
            "route": f"/candidates/{cop.candidate_id}",
            "entity_type": "candidate",
            "entity_id": str(cop.candidate_id),
            "label": "Candidate Priya Raman",
        },
    )
    turn, _ = await ask(cop.admin, conv["id"], "Draft outreach for this candidate", _send(cop))
    props = await _proposals(cop, turn["run_id"])
    assert len(props) == 1
    p = props[0]
    assert p.origin == "copilot" and p.status == "pending" and p.tool_key == "messaging.send"
    assert p.assignee_user_id == cop.admin.user_id and p.owner_user_id == cop.admin.user_id
    async with tenant_session(cop.tenant_id) as db:
        cfg = (await db.execute(select(AgentConfig).where(AgentConfig.agent_key == "copilot"))).scalar_one()
    assert cfg.proposal_expiry_hours == 24
    assert p.expires_at - p.created_at <= timedelta(hours=24, minutes=1)
    ans = answer(await messages(cop.admin, conv["id"]))
    assert ans["content"]["drafts"] == [str(p.id)]
    assert ["candidate", str(cop.candidate_id)] in ans["content"]["refs"]
    # nothing sent before the click; the Proposal API refuses "decide" for Copilot drafts
    resp = await cop.admin.client.post(
        f"/api/v1/proposals/{p.id}/decide",
        json={"decision": "approve"},
        headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "use_confirm"
    resp = await cop.admin.client.post(
        f"/api/v1/proposals/{p.id}/confirm", headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex})
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executed", resp.json()
    if resp.json()["status"] == "executed":
        async with tenant_session(cop.tenant_id) as db:
            rows = list(
                (await db.execute(select(AuditLog).where(AuditLog.via_proposal_id == p.id))).scalars()
            )
        assert rows and all(r.actor_type == "user" and r.actor_id == cop.admin.user_id for r in rows)
    msgs = await messages(cop.admin, conv["id"])
    event = msgs[-1]
    assert event["role"] == "event" and event["content"]["kind"] in ("draft_confirmed", "draft_failed")


async def test_task_draft_discard_and_expiry(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    call = {
        "title": "Call Priya",
        "entity_type": "candidate",
        "entity_id": str(cop.candidate_id),
        "due_in_hours": 4,
    }
    turn, _ = await ask(
        cop.admin,
        conv["id"],
        "Create a follow-up task",
        [{"kind": "tool_calls", "calls": [["tasks.create", call]]}, {"kind": "text", "text": "Draft ready."}],
    )
    p = (await _proposals(cop, turn["run_id"]))[0]
    resp = await cop.admin.client.post(f"/api/v1/proposals/{p.id}/discard", headers=h(cop.admin))
    assert resp.status_code == 200 and resp.json()["status"] == "discarded"
    assert (await messages(cop.admin, conv["id"]))[-1]["content"]["kind"] == "draft_discarded"
    turn2, _ = await ask(
        cop.admin,
        conv["id"],
        "Again",
        [
            {"kind": "tool_calls", "calls": [["tasks.create", {**call, "title": "Call Priya again"}]]},
            {"kind": "text", "text": "Draft ready."},
        ],
    )
    p2 = (await _proposals(cop, turn2["run_id"]))[0]
    async with tenant_session(cop.tenant_id) as db:
        await db.execute(
            update(AgentProposal)
            .where(AgentProposal.id == p2.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    async with tenant_session(cop.tenant_id) as db:
        assert await runtime_proposals.expire_due(db) >= 1
    msgs = await messages(cop.admin, conv["id"])
    assert msgs[-1]["content"]["kind"] == "draft_expired"


async def test_slot_proposal_stub_fails_not_available(cop: CopilotOrg) -> None:
    # an application the user can see is needed only at confirm time; the draft is created from the args
    conv = await new_conversation(cop.admin)
    app_id = str(uuid.uuid4())
    turn, _ = await ask(
        cop.admin, conv["id"], "Propose slots",
        [{"kind": "tool_calls", "calls": [["copilot.propose_slots", {"application_id": app_id, "duration_minutes": 45}]]}, {"kind": "text", "text": "Draft ready."}],
    )  # fmt: skip
    ans = answer(await messages(cop.admin, conv["id"]))
    tool = ans["content"]["tools"][0]
    # capability tools are offered only when the capability is on (C-4: agent_copilot on, no extra permission)
    assert tool["tool_key"] == "copilot.propose_slots"
    props = await _proposals(cop, turn["run_id"])
    assert len(props) == 1 and props[0].tool_key == "copilot.propose_slots"
    resp = await cop.admin.client.post(
        f"/api/v1/proposals/{props[0].id}/confirm",
        headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "execution_failed" and resp.json()["error_code"] in (
        "not_available",
        "not_found",
    )


async def test_close_discards_pending_drafts(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    call = {
        "title": "Ping",
        "entity_type": "candidate",
        "entity_id": str(cop.candidate_id),
        "due_in_hours": 2,
    }
    turn, _ = await ask(
        cop.admin,
        conv["id"],
        "task",
        [{"kind": "tool_calls", "calls": [["tasks.create", call]]}, {"kind": "text", "text": "ok"}],
    )
    assert (
        await cop.admin.client.post(f"{C}/conversations/{conv['id']}/close", headers=h(cop.admin))
    ).status_code == 200
    p = (await _proposals(cop, turn["run_id"]))[0]
    assert p.status == "discarded"
