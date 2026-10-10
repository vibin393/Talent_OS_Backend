# ruff: noqa: E501
"""Epic 78 story 78.4 (FR-404, AD-90): onboarding Q&A messages are written sealed only (bytes at rest, no plaintext
column), round-trip under the Tenant DEK, the AAD binds the row id (a blob copied to another row does not open), another
Tenant's key cannot open it, the length limit is enforced before sealing, topic labels are scrubbed and there is no
full-text index on the message table."""

import uuid

import pytest
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.knowledge import OnboardingQaMessage
from app.services.knowledge import sealing
from tests.agents.conftest import AgentOrg
from tests.modules.knowledge.helpers import E62Seed, enable, grant_ai, seed_worker, start_thread

QUESTION = "Hi, I am Ravi Kumar (ravi.kumar@example.com, 9876543210). When is my laptop issued?"


async def _asked(org: AgentOrg, question: str = QUESTION) -> tuple[E62Seed, uuid.UUID]:
    seed = await seed_worker(org, start_in=10)
    await enable(org.tenant_id, "onboarding", on=False)  # the unavailable path: no model call at all
    await grant_ai(org.tenant_id, seed.candidate_id, "ai_processing", "knowledge_assistant")
    thread = await start_thread(org.tenant_id, seed.candidate_id, question)
    return seed, thread.id


async def _rows(org: AgentOrg, thread_id: uuid.UUID) -> list[OnboardingQaMessage]:
    async with tenant_session(org.tenant_id) as db:
        rows = list((await db.execute(select(OnboardingQaMessage).where(OnboardingQaMessage.thread_id == thread_id).order_by(OnboardingQaMessage.seq))).scalars())
        for r in rows:
            db.expunge(r)
    return rows


async def test_new_messages_are_sealed_only(agent_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org)
    async with tenant_session(agent_org.tenant_id) as db:
        cols = {r[0] for r in await db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'onboarding_qa_messages'"))}
        raw = (await db.execute(text("SELECT text_sealed, feedback_note_sealed FROM onboarding_qa_messages WHERE thread_id = :t ORDER BY seq"), {"t": tid})).all()
    assert "text" not in cols and "feedback_note" not in cols
    assert raw and all(isinstance(r[0], bytes | memoryview) for r in raw)
    assert all(b"laptop" not in bytes(r[0]) for r in raw)


async def test_round_trip_and_labels(agent_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org)
    rows = await _rows(agent_org, tid)
    asker = rows[0]
    async with tenant_session(agent_org.tenant_id) as db:
        assert await sealing.open_message(db, agent_org.tenant_id, asker.id, asker.text_sealed) == QUESTION
    assert asker.question_fingerprint and len(asker.question_fingerprint) >= 32
    label = asker.topic_label or ""
    assert label.startswith("general:") and "laptop" in label
    for leak in ("ravi", "kumar", "9876", "@", "example"):
        assert leak not in label.lower(), label


async def test_aad_binds_the_row_id(agent_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org)
    asker, system = (await _rows(agent_org, tid))[:2]
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(Exception):  # noqa: B017 - InvalidTag from AES-GCM
            await sealing.open_message(db, agent_org.tenant_id, system.id, asker.text_sealed)


async def test_another_tenants_key_cannot_open(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    _, tid = await _asked(agent_org)
    asker = (await _rows(agent_org, tid))[0]
    async with tenant_session(other_org.tenant_id) as db:
        await sealing.seal_message(db, other_org.tenant_id, uuid.uuid4(), "creates the other tenant's key")
    async with tenant_session(other_org.tenant_id) as db:
        with pytest.raises(Exception):  # noqa: B017
            await sealing.open_message(db, other_org.tenant_id, asker.id, asker.text_sealed)


async def test_length_is_enforced_before_sealing(agent_org: AgentOrg) -> None:
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(ValidationFailed):
            await sealing.seal_message(db, agent_org.tenant_id, uuid.uuid4(), "x" * 2001)


async def test_feedback_note_is_sealed(agent_org: AgentOrg) -> None:
    from app.services import knowledge_service

    seed, tid = await _asked(agent_org)
    system = (await _rows(agent_org, tid))[1]
    async with tenant_session(agent_org.tenant_id) as db:
        await db.execute(text("UPDATE onboarding_qa_messages SET role = 'assistant' WHERE id = :i"), {"i": system.id})
    async with tenant_session(agent_org.tenant_id) as db:
        await knowledge_service.qa.feedback(db, seed.candidate_id, system.id, "incorrect", "My manager Anil said otherwise")
    row = next(r for r in await _rows(agent_org, tid) if r.id == system.id)
    assert row.feedback_note_sealed is not None and b"Anil" not in bytes(row.feedback_note_sealed)
    async with tenant_session(agent_org.tenant_id) as db:
        assert await sealing.open_note(db, agent_org.tenant_id, row.id, row.feedback_note_sealed) == "My manager Anil said otherwise"


async def test_no_full_text_index_on_messages(agent_org: AgentOrg) -> None:
    async with tenant_session(agent_org.tenant_id) as db:
        defs = [r[0].lower() for r in await db.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'onboarding_qa_messages'"))]
    assert defs and not any("gin" in d or "tsvector" in d or "to_tsvector" in d for d in defs), defs


def test_scrub_and_relabel() -> None:
    assert sealing.topic_label(None, "When will Priya get the form at Infosys?") == "general: form"
    label = sealing.topic_label("pay", "What is my joining bonus? Call me on +91 98765 43210")
    assert label == "pay: joining bonus call"
    assert sealing.relabel("general: joining bonus", "pay") == "pay: joining bonus"
    assert sealing.relabel("general", "tax") == "tax"
