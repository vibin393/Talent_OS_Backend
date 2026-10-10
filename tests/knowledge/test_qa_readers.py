# ruff: noqa: E501
"""Epic 78 story 78.4 (FR-404): decryption only for the allowed readers -- the asker's own thread, the coordinator's
thread view, the gap report (representative text only for ``knowledge:manage``), the erasure export. Inbox list views
show the non-reversible ``topic_label`` instead of the question."""

import uuid

from fastapi import FastAPI
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal, Scope
from app.services.knowledge import gaps
from app.services.knowledge_erasure import OnboardingQaHandler
from app.services.privacy.registry import ErasureContext
from tests.agents.conftest import AgentOrg
from tests.knowledge.test_sealing import QUESTION, _asked
from tests.p2 import sign_in_candidate


async def test_asker_sees_own_text(app: FastAPI, agent_org: AgentOrg) -> None:
    seed, tid = await _asked(agent_org)
    worker = await sign_in_candidate(app, seed.candidate_id, agent_org.tenant_id)
    view = (await worker.get(f"/api/v1/portal/work/help/threads/{tid}")).json()
    assert view["messages"][0]["text"] == QUESTION
    state = (await worker.get("/api/v1/portal/work/help")).json()
    assert state["threads"][0]["first_question"] == QUESTION[:200]
    await worker.aclose()


async def test_coordinator_thread_view_decrypts_and_list_shows_label(agent_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org)
    m = agent_org.admin
    one = (await m.client.get(f"/api/v1/onboarding-qa/threads/{tid}")).json()
    assert one["messages"][0]["text"] == QUESTION and one["last_question"] == QUESTION
    listing = (await m.client.get("/api/v1/onboarding-qa/threads")).json()
    row = next(t for t in listing if t["id"] == str(tid))
    assert row["last_question"] and row["last_question"].startswith("general:") and "Ravi" not in row["last_question"]


async def test_gap_report_decrypts_only_for_knowledge_manage(agent_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org, "What is my joining bonus amount?")
    manage = RequestContext(tenant_id=agent_org.tenant_id, principal=Principal(type="user", id=agent_org.admin.user_id, tenant_id=agent_org.tenant_id, permissions={"knowledge:manage": Scope.TENANT}))
    onboarding_only = RequestContext(tenant_id=agent_org.tenant_id, principal=Principal(type="user", id=agent_org.admin.user_id, tenant_id=agent_org.tenant_id, permissions={"onboarding:manage": Scope.TENANT}))
    async with tenant_session(agent_org.tenant_id) as db:
        full = await gaps.gap_report(manage, db)
        labelled = await gaps.gap_report(onboarding_only, db)
    assert full[0].question == "What is my joining bonus amount?" and full[0].times_asked == 1
    assert labelled[0].question == labelled[0].topic_label and "joining bonus" in (labelled[0].topic_label or "")
    assert "What is my" not in labelled[0].question
    del tid


async def test_erasure_export_decrypts(agent_org: AgentOrg) -> None:
    seed, _ = await _asked(agent_org)
    ctx = ErasureContext(tenant_id=agent_org.tenant_id, candidate_id=seed.candidate_id, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(agent_org.tenant_id) as db:
        out = await OnboardingQaHandler().export(db, ctx)
    assert out is not None and out["onboarding_questions"][0]["text"] == QUESTION


async def test_logs_never_contain_message_text(agent_org: AgentOrg, caplog: object) -> None:
    from loguru import logger

    seen: list[str] = []
    sink = logger.add(lambda msg: seen.append(str(msg)), level="DEBUG")
    try:
        await _asked(agent_org, "My secret question about maternity leave please")
    finally:
        logger.remove(sink)
    assert not any("maternity" in line for line in seen)
    async with tenant_session(agent_org.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM onboarding_qa_messages WHERE topic_label IS NOT NULL"))).scalar_one() >= 1
    del caplog
