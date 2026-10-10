# ruff: noqa: E501
"""Epic 54 privacy (54.5): own conversations only, delete / purge remove content + run payloads and discard drafts,
feedback survives deletion, candidate erasure makes citing turns unreadable, admin usage carries no text."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import func, select, update

from app.core.database import tenant_session
from app.models.agent_runtime import AgentPayload, AgentProposal
from app.models.copilot import CopilotConversation, CopilotFeedback, CopilotMessage
from app.schemas.copilot import CopilotUsageOut, UsageCommentOut, UsageDayOut
from app.services import copilot_service
from app.services.copilot_erasure import CopilotMessagesHandler
from app.services.privacy.registry import ErasureContext
from tests.agents.copilot.conftest import C, CopilotOrg, answer, ask, h, messages, new_conversation
from tests.modules.identity.conftest import join


def _task_turn(org: CopilotOrg) -> list[dict]:
    cid = str(org.candidate_id)
    return [
        {"kind": "tool_calls", "calls": [["candidates.get", {"candidate_id": cid}]]},
        {
            "kind": "tool_calls",
            "calls": [
                [
                    "tasks.create",
                    {"title": "Call Priya", "entity_type": "candidate", "entity_id": cid, "due_in_hours": 4},
                ]
            ],
        },
        {
            "kind": "text",
            "text": f"Priya Raman is a [candidate](/candidates/{cid}). I've prepared a draft for you to review.",
        },
    ]


async def _payloads(org: CopilotOrg, run_id: str) -> int:
    async with tenant_session(org.tenant_id) as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AgentPayload)
                    .where(AgentPayload.run_id == uuid.UUID(run_id))
                )
            ).scalar_one()
        )


async def test_delete_removes_content_payloads_and_drafts_but_keeps_feedback(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(cop.admin, conv["id"], "Summarise this candidate and add a task", _task_turn(cop))
    assert await _payloads(cop, turn["run_id"]) > 0
    resp = await cop.admin.client.post(
        f"{C}/turns/{turn['run_id']}/feedback",
        json={"rating": "unhelpful", "comment": "Too long"},
        headers=h(cop.admin),
    )
    assert resp.status_code == 200 and resp.json()["rating"] == "unhelpful"
    resp = await cop.admin.client.delete(f"{C}/conversations/{conv['id']}", headers=h(cop.admin))
    assert resp.status_code == 204, resp.text
    assert (await cop.admin.client.get(f"{C}/conversations/{conv['id']}")).status_code == 404
    assert await _payloads(cop, turn["run_id"]) == 0
    async with tenant_session(cop.tenant_id) as db:
        msgs = (
            await db.execute(
                select(func.count())
                .select_from(CopilotMessage)
                .where(CopilotMessage.conversation_id == uuid.UUID(conv["id"]))
            )
        ).scalar_one()
        fb = (
            await db.execute(
                select(CopilotFeedback).where(CopilotFeedback.run_id == uuid.UUID(turn["run_id"]))
            )
        ).scalar_one()
        prop = (
            await db.execute(select(AgentProposal).where(AgentProposal.run_id == uuid.UUID(turn["run_id"])))
        ).scalar_one()
    assert msgs == 0
    assert fb.conversation_id is None and fb.rating == "unhelpful"
    assert prop.status == "discarded"


async def test_comments_disabled(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(cop.admin, conv["id"], "hello")
    assert (
        await cop.admin.client.put(
            f"{C}/settings", json={"allow_feedback_comments": False}, headers=h(cop.admin)
        )
    ).status_code == 200
    resp = await cop.admin.client.post(
        f"{C}/turns/{turn['run_id']}/feedback",
        json={"rating": "unhelpful", "comment": "x"},
        headers=h(cop.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "comments_disabled"
    resp = await cop.admin.client.post(
        f"{C}/turns/{turn['run_id']}/feedback", json={"rating": "helpful"}, headers=h(cop.admin)
    )
    assert resp.status_code == 200


async def test_purge_expired(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(cop.admin, conv["id"], "hello", [{"kind": "text", "text": "Hi."}])
    async with tenant_session(cop.tenant_id) as db:
        await db.execute(
            update(CopilotConversation)
            .where(CopilotConversation.id == uuid.UUID(conv["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert await copilot_service.purge_expired(cop.tenant_id) == 1
    assert (await cop.admin.client.get(f"{C}/conversations/{conv['id']}")).status_code == 404
    assert await _payloads(cop, turn["run_id"]) == 0


async def test_candidate_erasure_redacts_citing_turns(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    await ask(cop.admin, conv["id"], "Tell me about Priya", _task_turn(cop))
    other = await new_conversation(cop.admin)
    await ask(cop.admin, other["id"], "Unrelated question", [{"kind": "text", "text": "Fine."}])
    async with tenant_session(cop.tenant_id) as db:
        ctx = ErasureContext(
            tenant_id=cop.tenant_id, candidate_id=cop.candidate_id, run_id=uuid.uuid4(), reason="dsr"
        )
        out = await CopilotMessagesHandler().erase(db, ctx)
        assert out["copilot_messages"] >= 1
        assert await CopilotMessagesHandler().verify(db, ctx) == []
    msgs = await messages(cop.admin, conv["id"])
    citing = [m for m in msgs if m["role"] == "assistant"]
    assert citing and all(
        m["redacted"] and m["content"] == {"redacted": True, "role": "assistant"} for m in citing
    )
    assert (await cop.admin.client.get(f"{C}/conversations/{conv['id']}")).json()[
        "title"
    ] == "Redacted conversation"
    untouched = answer(await messages(cop.admin, other["id"]))
    assert untouched["redacted"] is False


async def test_admin_usage_is_metadata_only(cop: CopilotOrg, app: FastAPI) -> None:
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(
        cop.admin,
        conv["id"],
        "A private question about Priya",
        [{"kind": "text", "text": "A private answer."}],
    )
    await cop.admin.client.post(
        f"{C}/turns/{turn['run_id']}/feedback",
        json={"rating": "helpful", "comment": "Nice"},
        headers=h(cop.admin),
    )
    resp = await cop.admin.client.get(f"{C}/admin/usage")
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "private question" not in text and "private answer" not in text
    body = resp.json()
    assert sum(d["turns"] for d in body["days"]) >= 1 and sum(d["helpful"] for d in body["days"]) == 1
    assert body["comments"][0]["comment"] == "Nice"
    # schema guard: no free-text fields beyond the user's own feedback comment
    for model in (CopilotUsageOut, UsageDayOut):
        assert not {"text", "question", "answer", "content", "title"} & set(model.model_fields)
    assert set(UsageCommentOut.model_fields) == {"run_id", "rating", "comment", "created_at"}
    viewer = await join(app, cop.tenant, "viewer", name="Vic Viewer")
    try:
        assert (await viewer.client.get(f"{C}/admin/usage")).status_code == 403
        status = (await viewer.client.get(f"{C}/status")).json()
        assert status["available"] is False and status["reason"] == "no_permission"
        assert (await viewer.client.get(f"{C}/conversations")).status_code == 403
    finally:
        await viewer.client.aclose()
