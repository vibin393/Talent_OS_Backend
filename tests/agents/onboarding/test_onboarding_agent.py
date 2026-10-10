# ruff: noqa: E501
"""Epic 62 stories 62.4 / 62.5 with the Fake LLM: grounded answers with citations (executed in a tainted run), resume on
the next question, the ungrounded-claim refusal and hand-off, pay questions, thread binding, the portal consent gate,
cross-Worker 404s, the unavailable path, feedback, the FAQ gap report and unpublishing."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.models.knowledge import OnboardingQaMessage, OnboardingQaThread
from app.models.tasks import Task
from app.services import knowledge_service
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.knowledge import sealing
from tests.agents.conftest import AgentOrg
from tests.modules.knowledge.helpers import E62Seed, enable, grant_ai, publish_article, seed_worker
from tests.p2 import sign_in_candidate

LAPTOP = "Laptops are issued on day one at the Pune office."
CSRF = {"X-CSRF-Token": "csrf-token"}


async def _ready(org: AgentOrg, *, agent: bool = True) -> tuple[E62Seed, uuid.UUID]:
    seed = await seed_worker(org, start_in=10)
    await enable(org.tenant_id, "onboarding", on=agent)
    await grant_ai(org.tenant_id, seed.candidate_id, "ai_processing", "knowledge_assistant")
    _, chunk = await publish_article(org.tenant_id, org.ctx(), "IT FAQ", f"# IT\n\n{LAPTOP}")
    return seed, chunk


async def _thread(org: AgentOrg, seed: E62Seed) -> uuid.UUID:
    async with tenant_session(org.tenant_id) as db:
        elig = await knowledge_service.qa.eligibility(db, seed.candidate_id)
        assert elig is not None
        t = OnboardingQaThread(
            id=uuid.uuid4(), tenant_id=org.tenant_id, candidate_id=seed.candidate_id, placement_id=elig.placement_id,
            onboarding_case_id=elig.case_id, audience=elig.audience, status="open", coordinator_user_id=elig.coordinator_id,
        )  # fmt: skip
        db.add(t)
    return t.id


async def _ask(org: AgentOrg, seed: E62Seed, thread_id: uuid.UUID, text: str) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return await knowledge_service.qa.ask(db, org.tenant_id, seed.candidate_id, text, thread_id)


async def _calls(org: AgentOrg, run_id: uuid.UUID) -> dict[str, list[AgentToolCall]]:
    async with tenant_session(org.tenant_id) as db:
        out: dict[str, list[AgentToolCall]] = {}
        for c in (
            await db.execute(
                select(AgentToolCall).where(AgentToolCall.run_id == run_id).order_by(AgentToolCall.step_no)
            )
        ).scalars():
            db.expunge(c)
            out.setdefault(c.tool_key, []).append(c)
    return out


def _search(q: str = "laptop") -> FakeTurn:
    return FakeTurn(
        "tool_calls", calls=(("knowledge.search", {"query": q, "audience": "candidate", "limit": 3}),)
    )


def _wait(thread_id: uuid.UUID) -> FakeTurn:
    return FakeTurn(
        "await_event",
        wait={"condition": "reply", "ref": str(thread_id), "hours": 72, "reason": "next question"},
    )


async def test_grounded_answer_and_resume(agent_org: AgentOrg) -> None:
    seed, chunk = await _ready(agent_org)
    tid = await _thread(agent_org, seed)
    script = [
        _search(),
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "onboarding.reply_grounded",
                    {"thread_id": str(tid), "answer": LAPTOP, "citations": [f"kb:{chunk}"], "topic": "it"},
                ),
            ),
        ),
        _wait(tid),
        FakeTurn(
            "emit_result", result={"verdict": "benign", "reasons": []}
        ),  # the resume pre-screen (fast tier)
        FakeTurn(
            "tool_calls",
            calls=(
                ("onboarding.hand_off", {"thread_id": str(tid), "reason": "sensitive_topic", "topic": "pay"}),
            ),
        ),
        _wait(tid),
    ]
    with fake_llm(script):
        out = await _ask(agent_org, seed, tid, "When do I get my laptop?")
        async with tenant_session(agent_org.tenant_id) as db:
            thread = await db.get(OnboardingQaThread, tid)
            run = await db.get(AgentRun, thread.agent_run_id)
            first_run = run.id
        calls0 = await _calls(agent_org, first_run)
        assert run.status == "waiting" and run.tainted, (
            run.status,
            run.reason_code,
            {k: [(c.outcome, c.error_code) for c in v] for k, v in calls0.items()},
        )
        calls = await _calls(agent_org, first_run)
        assert calls["onboarding.reply_grounded"][0].outcome == "executed", calls[
            "onboarding.reply_grounded"
        ][0].error_code
        assert calls["onboarding.reply_grounded"][0].mode_reason != "tainted_egress"
        out = await _ask(agent_org, seed, tid, "What is my CTC?")
    del out
    async with tenant_session(agent_org.tenant_id) as db:
        thread = await db.get(OnboardingQaThread, tid)
        msgs = list(
            (
                await db.execute(
                    select(OnboardingQaMessage)
                    .where(OnboardingQaMessage.thread_id == tid)
                    .order_by(OnboardingQaMessage.seq)
                )
            ).scalars()
        )
        tasks = list((await db.execute(select(Task).where(Task.entity_id == seed.candidate_id))).scalars())
        await sealing.open_messages(db, msgs)  # Epic 78: message text is sealed at rest
    assert [t.title for t in tasks if t.title.startswith("Onboarding question")], [t.title for t in tasks]
    task = next(t for t in tasks if t.title.startswith("Onboarding question"))
    assert thread.agent_run_id == first_run  # the second question resumed the same run
    roles = [(m.role, m.outcome) for m in msgs]
    assert roles == [("asker", None), ("assistant", "answered"), ("asker", None), ("system", "handed_off")]
    assert msgs[1].citations[0]["citation"] == f"kb:{chunk}" and msgs[1].citations[0]["title"] == "IT FAQ"
    assert "AI assistant" in msgs[1].plain_text
    assert msgs[3].plain_text == "I'll pass this to your coordinator." and msgs[3].topic == "pay"
    assert task.owner_id == agent_org.admin.user_id and thread.status == "handed_off"


async def test_ungrounded_claim_refused_then_hand_off_and_gap(agent_org: AgentOrg) -> None:
    seed, chunk = await _ready(agent_org)
    tid = await _thread(agent_org, seed)
    script = [
        _search("joining bonus"),
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "onboarding.reply_grounded",
                    {
                        "thread_id": str(tid),
                        "answer": "Your joining bonus is ₹50,000.",
                        "citations": [f"kb:{chunk}"],
                        "topic": "pay",
                    },
                ),
            ),
        ),
        FakeTurn(
            "tool_calls",
            calls=(("onboarding.hand_off", {"thread_id": str(tid), "reason": "ungrounded", "topic": "pay"}),),
        ),
        _wait(tid),
    ]
    with fake_llm(script):
        await _ask(agent_org, seed, tid, "What is my joining bonus?")
    async with tenant_session(agent_org.tenant_id) as db:
        thread = await db.get(OnboardingQaThread, tid)
    calls = await _calls(agent_org, thread.agent_run_id)
    assert calls["onboarding.reply_grounded"][0].outcome == "invalid"
    assert calls["onboarding.reply_grounded"][0].error_code == "ungrounded_claim"
    assert calls["onboarding.hand_off"][0].outcome == "executed"
    resp = await agent_org.admin.client.get("/api/v1/knowledge/gaps")
    assert resp.status_code == 200, resp.text
    gap = resp.json()[0]
    assert (
        gap["question"] == "What is my joining bonus?"
        and gap["outcome"] == "ungrounded"
        and gap["topic"] == "pay"
    )


async def test_other_thread_and_foreign_citations_are_not_found(agent_org: AgentOrg) -> None:
    seed, _ = await _ready(agent_org)
    _, internal_chunk = await publish_article(
        agent_org.tenant_id,
        agent_org.ctx(),
        "Internal margin policy",
        "# Margins\n\nTarget margin is 20%.",
        audience="internal",
    )
    tid = await _thread(agent_org, seed)
    other = uuid.uuid4()
    script = [
        FakeTurn("tool_calls", calls=(("onboarding.get_thread", {"thread_id": str(other)}),)),
        FakeTurn(
            "tool_calls",
            calls=(("knowledge.search", {"query": "margin", "audience": "internal", "limit": 3}),),
        ),
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "onboarding.reply_grounded",
                    {
                        "thread_id": str(tid),
                        "answer": "Target margin is 20%.",
                        "citations": [f"kb:{internal_chunk}"],
                        "topic": "policy",
                    },
                ),
            ),
        ),
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "onboarding.reply_grounded",
                    {
                        "thread_id": str(tid),
                        "answer": "See your task.",
                        "citations": [f"task:{uuid.uuid4()}"],
                        "topic": "documents",
                    },
                ),
            ),
        ),
        FakeTurn("emit_result", result={"outcome": "needs_human", "summary": "x"}),
    ]
    with fake_llm(script):
        await _ask(agent_org, seed, tid, "Show me Priya's bank details")
    async with tenant_session(agent_org.tenant_id) as db:
        thread = await db.get(OnboardingQaThread, tid)
    calls = await _calls(agent_org, thread.agent_run_id)
    assert calls["onboarding.get_thread"][0].outcome == "not_found"
    assert calls["knowledge.search"][0].result_summary == "0 knowledge hits"
    assert [c.outcome for c in calls.get("onboarding.reply_grounded", [])][:1] == ["not_found"]


async def test_portal_consent_gate_404_feedback_and_unavailable(app: FastAPI, agent_org: AgentOrg) -> None:
    seed, _ = await _ready(agent_org, agent=False)
    async with tenant_session(agent_org.tenant_id) as db:
        from app.models.privacy import Consent

        await db.execute(
            Consent.__table__.delete().where(
                Consent.candidate_id == seed.candidate_id, Consent.purpose == "knowledge_assistant"
            )
        )
    worker = await sign_in_candidate(app, seed.candidate_id, agent_org.tenant_id)
    state = (await worker.get("/api/v1/portal/work/help")).json()
    assert (
        state["available"]
        and state["consent_needed"]
        and state["notice"]["title"] == "Onboarding help assistant"
    )
    blocked = await worker.post(
        "/api/v1/portal/work/help/threads", json={"text": "Where do I upload my PAN?"}, headers=CSRF
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "consent_required"
    ok = await worker.post("/api/v1/portal/work/help/consent", json={"decision": "accept"}, headers=CSRF)
    assert ok.status_code == 200 and ok.json()["consent_needed"] is False
    # the agent is off -> unavailable message + coordinator task
    resp = await worker.post(
        "/api/v1/portal/work/help/threads", json={"text": "Where do I upload my PAN?"}, headers=CSRF
    )
    assert resp.status_code == 201, resp.text
    thread = resp.json()
    assert (
        thread["messages"][-1]["text"]
        == "The onboarding assistant is not available. Your question was sent to your coordinator."
    )
    async with tenant_session(agent_org.tenant_id) as db:
        assert (await db.execute(select(Task).where(Task.entity_id == seed.candidate_id))).first() is not None
    # a coordinator reply (AI off) and feedback
    reply = await agent_org.admin.client.post(
        f"/api/v1/onboarding-qa/threads/{thread['id']}/reply",
        json={"text": "Upload it under My Work > Onboarding."},
        headers=agent_org.admin.h(),
    )
    assert reply.status_code == 200, reply.text
    view = (await worker.get(f"/api/v1/portal/work/help/threads/{thread['id']}")).json()
    coord = next(m for m in view["messages"] if m["author"] == "coordinator")
    fb = await worker.post(
        f"/api/v1/portal/work/help/messages/{coord['id']}/feedback",
        json={"feedback": "helpful"},
        headers=CSRF,
    )
    assert fb.status_code == 200 and any(m["feedback"] == "helpful" for m in fb.json()["messages"])
    # another Worker cannot see it
    from tests.epic3 import create_candidate

    other = await create_candidate(agent_org.admin, full_name="Ravi Other")
    other_client = await sign_in_candidate(app, uuid.UUID(other["id"]), agent_org.tenant_id)
    assert (await other_client.get(f"/api/v1/portal/work/help/threads/{thread['id']}")).status_code == 404
    assert (
        await other_client.post(
            f"/api/v1/portal/work/help/threads/{thread['id']}/messages", json={"text": "hi"}, headers=CSRF
        )
    ).status_code in (404, 409)
    assert (
        await other_client.post(
            f"/api/v1/portal/work/help/messages/{coord['id']}/feedback",
            json={"feedback": "helpful"},
            headers=CSRF,
        )
    ).status_code == 404
    assert (
        await other_client.post(f"/api/v1/portal/work/help/threads/{thread['id']}/handoff", headers=CSRF)
    ).status_code == 404
    await worker.aclose()
    await other_client.aclose()


async def test_unpublished_source_is_never_returned(agent_org: AgentOrg) -> None:
    from app.core.permissions import CATALOG, Principal, Scope

    seed, _ = await _ready(agent_org)
    del seed
    ctx = agent_org.ctx()
    import dataclasses

    ctx = dataclasses.replace(
        ctx,
        principal=Principal(
            type="user",
            id=agent_org.admin.user_id,
            tenant_id=agent_org.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        ),
    )
    async with tenant_session(agent_org.tenant_id) as db:
        hits = await knowledge_service.search(ctx, db, "laptop", audience="worker")
        assert hits and hits[0].title == "IT FAQ" and hits[0].citation.startswith("kb:")
        await knowledge_service.sources.unpublish(ctx, db, hits[0].source_id)
        assert await knowledge_service.search(ctx, db, "laptop", audience="worker") == []
