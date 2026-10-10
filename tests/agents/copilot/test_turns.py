# ruff: noqa: E501
"""Epic 54 turns and streaming (54.1): one run per question on ``conversations``, owner = the user, stored answer,
SSE order / resume / headers, unavailable reasons, rate limit, closed conversations, idempotent replay."""

import uuid

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentRun
from app.services import ai
from app.services.copilot import ratelimit
from tests.agents.copilot.conftest import (
    C,
    CopilotOrg,
    answer,
    ask,
    enable,
    h,
    messages,
    new_conversation,
    sse_events,
)


async def _run(org: CopilotOrg, run_id: str) -> AgentRun:
    async with tenant_session(org.tenant_id) as db:
        run = await db.get(AgentRun, uuid.UUID(run_id))
        assert run is not None
        db.expunge(run)
        return run


async def test_status_available_and_modes(cop: CopilotOrg) -> None:
    resp = await cop.admin.client.get(f"{C}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True and body["reason"] is None
    assert body["questions_per_hour"] == 60 and body["questions_left_this_hour"] == 60
    assert body["modes"][0] == {"agent_key": "copilot", "label": "Copilot"}


async def test_turn_runs_as_user_and_stores_answer(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin, page_context={"route": "/candidates", "label": "Candidates"})
    turn, fake = await ask(
        cop.admin, conv["id"], "Which open demands are at risk of missing SLA?",
        [{"kind": "tool_calls", "calls": [["sla.at_risk", {}]]}, {"kind": "text", "text": "I couldn't find any matching records you have access to."}],
    )  # fmt: skip
    assert (
        turn["status"] == "queued" and turn["stream_url"] == f"/api/v1/copilot/turns/{turn['run_id']}/events"
    )
    run = await _run(cop, turn["run_id"])
    assert run.status == "completed", (run.status, run.reason_code)
    assert run.owner_user_id == cop.admin.user_id
    assert (
        run.origin == "copilot"
        and run.trigger_kind == "copilot"
        and run.conversation_id == uuid.UUID(conv["id"])
    )
    first = fake.calls[0]
    names = {t["name"] for t in first["tools"]}
    assert "sla__at_risk" in names and "messaging__send" in names
    assert "messaging__create_draft" not in names and "flags__raise" not in names
    assert first["output_config"] == {"effort": "low"}
    msgs = await messages(cop.admin, conv["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    ans = answer(msgs)
    assert ans["content"]["status"] == "completed"
    assert "couldn't find" in ans["content"]["text"]
    assert (
        ans["content"]["tools"][0]["tool_key"] == "sla.at_risk"
        and ans["content"]["tools"][0]["outcome"] == "executed"
    )
    # follow-up turn: parent run and prior turns as untrusted context
    turn2, fake2 = await ask(
        cop.admin, conv["id"], "And this week?", [{"kind": "text", "text": "Nothing new."}]
    )
    run2 = await _run(cop, turn2["run_id"])
    assert run2.parent_run_id == run.id and run2.tainted is True
    kickoff = fake2.calls[0]["messages"][0]["content"][0]["text"]
    assert "untrusted_document" in kickoff and "Which open demands" in kickoff


async def test_stream_order_resume_and_headers(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(
        cop.admin, conv["id"], "Create a follow-up task",
        [
            {"kind": "tool_calls", "calls": [["tasks.create", {"title": "Call Priya", "entity_type": "candidate", "entity_id": str(cop.candidate_id), "due_in_hours": 4}]]},
            {"kind": "text", "text": "I've prepared a draft for you to review."},
        ],
    )  # fmt: skip
    events, resp = await sse_events(cop.admin, turn["run_id"])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert (
        resp.headers["cache-control"] == "no-cache, no-transform"
        and resp.headers["x-accel-buffering"] == "no"
    )
    kinds = [e for _, e, _ in events]
    assert kinds[0] == "turn.started" and kinds[-1] == "turn.completed"
    assert "text.delta" in kinds and "tool.finished" in kinds and "draft.created" in kinds
    assert kinds.index("tool.finished") < kinds.index("turn.completed")
    draft = next(d for _, e, d in events if e == "draft.created")
    assert draft["tool_key"] == "tasks.create"
    # resume after the first event
    resumed, _ = await sse_events(cop.admin, turn["run_id"], last_event_id=events[0][0])
    assert [e for _, e, _ in resumed] == kinds[1:]


async def test_other_user_and_admin_cannot_read(cop: CopilotOrg, other_cop: CopilotOrg, app) -> None:  # noqa: ANN001
    from tests.modules.identity.conftest import join

    conv = await new_conversation(cop.admin)
    turn, _ = await ask(cop.admin, conv["id"], "hello")
    colleague = await join(app, cop.tenant, name="Colin Colleague")  # another Tenant Admin (holds ai:govern)
    try:
        for url in (
            f"{C}/conversations/{conv['id']}",
            f"{C}/conversations/{conv['id']}/messages",
            f"{C}/turns/{turn['run_id']}/events",
        ):
            assert (await colleague.client.get(url)).status_code == 404, url
            assert (await other_cop.admin.client.get(url)).status_code == 404, url
        listed = (await colleague.client.get(f"{C}/conversations")).json()
        assert listed["total"] == 0
        # payloads of a Copilot run are private
        resp = await colleague.client.post(
            f"/api/v1/agent-runs/{turn['run_id']}/payload",
            json={"reason": "governance review"},
            headers=h(colleague),
        )
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "copilot_private"
        assert (
            await colleague.client.post(f"{C}/turns/{turn['run_id']}/cancel", headers=h(colleague))
        ).status_code == 404
    finally:
        await colleague.client.aclose()


async def test_unavailable_reasons(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    async with tenant_session(cop.tenant_id) as db:
        await ai.set_features(db, cop.tenant_id, {"agent_copilot": False})
    body = (await cop.admin.client.get(f"{C}/status")).json()
    assert body["available"] is False and body["reason"] == "not_enabled"
    resp = await cop.admin.client.post(
        f"{C}/conversations/{conv['id']}/messages",
        json={"text": "hi"},
        headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "copilot_unavailable"
    assert resp.json()["error"]["details"]["reason"] == "not_enabled"
    await enable(cop.tenant_id)
    resp = await cop.admin.client.post(
        "/api/v1/ai/kill-switch", json={"on": True, "reason": "test"}, headers=h(cop.admin)
    )
    assert resp.status_code == 200, resp.text
    assert (await cop.admin.client.get(f"{C}/status")).json()["reason"] == "kill_switch"


async def test_rate_limit_429(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    resp = await cop.admin.client.put(f"{C}/settings", json={"questions_per_hour": 5}, headers=h(cop.admin))
    assert resp.status_code == 200, resp.text
    for i in range(5):
        await ask(cop.admin, conv["id"], f"q{i}")
    resp = await cop.admin.client.post(
        f"{C}/conversations/{conv['id']}/messages",
        json={"text": "one more"},
        headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert resp.status_code == 429, resp.text
    err = resp.json()["error"]
    assert err["code"] == "copilot_rate_limited" and err["details"]["retry_after_s"] > 0
    assert "limit of 5 questions an hour" in err["message"]
    assert int(resp.headers["retry-after"]) > 0
    await ratelimit.reset(cop.tenant_id, cop.admin.user_id)


async def test_closed_conversation_and_idempotent_replay(cop: CopilotOrg) -> None:
    conv = await new_conversation(cop.admin)
    key = uuid.uuid4().hex
    from app.services.ai.fake_llm import fake_llm
    from tests.agents.copilot.conftest import script

    with fake_llm(script({"kind": "text", "text": "Hi."})):
        first = await cop.admin.client.post(
            f"{C}/conversations/{conv['id']}/messages",
            json={"text": "hi"},
            headers=h(cop.admin, **{"Idempotency-Key": key}),
        )
        again = await cop.admin.client.post(
            f"{C}/conversations/{conv['id']}/messages",
            json={"text": "hi"},
            headers=h(cop.admin, **{"Idempotency-Key": key}),
        )
    assert first.status_code == again.status_code == 200
    assert first.json()["run_id"] == again.json()["run_id"]
    async with tenant_session(cop.tenant_id) as db:
        runs = list(
            (
                await db.execute(select(AgentRun).where(AgentRun.conversation_id == uuid.UUID(conv["id"])))
            ).scalars()
        )
    assert len(runs) == 1
    assert (
        await cop.admin.client.post(f"{C}/conversations/{conv['id']}/close", headers=h(cop.admin))
    ).status_code == 200
    resp = await cop.admin.client.post(
        f"{C}/conversations/{conv['id']}/messages",
        json={"text": "more"},
        headers=h(cop.admin, **{"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conversation_closed"


async def test_unknown_mode_and_rename(cop: CopilotOrg) -> None:
    resp = await cop.admin.client.post(
        f"{C}/conversations", json={"agent_key": "nonsense"}, headers=h(cop.admin)
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unknown_mode"
    conv = await new_conversation(cop.admin)
    resp = await cop.admin.client.patch(
        f"{C}/conversations/{conv['id']}", json={"title": "My REQ checks"}, headers=h(cop.admin)
    )
    assert resp.status_code == 200 and resp.json()["title"] == "My REQ checks"
