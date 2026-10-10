# ruff: noqa: E501
"""Epic 78 story 78.4 (FR-404): the resumable sealing backfill (the state between ``512_02`` and ``512_03``,
recreated here by re-adding the plaintext columns on this test DB): batches with a cursor, stop on the first failure
without NULLing plaintext, idempotent rerun, verify counts; after the drop it is a no-op."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import tenant_session
from app.services import vault_service
from app.services.knowledge import sealing
from tests.agents.conftest import AgentOrg
from tests.knowledge.test_sealing import _asked


@pytest.fixture
async def plaintext_columns(owner_db: AsyncEngine) -> AsyncIterator[None]:
    async with owner_db.begin() as conn:
        await conn.execute(text("ALTER TABLE onboarding_qa_messages ADD COLUMN IF NOT EXISTS text TEXT NULL"))
        await conn.execute(text("ALTER TABLE onboarding_qa_messages ADD COLUMN IF NOT EXISTS feedback_note VARCHAR(500) NULL"))
    try:
        yield
    finally:
        async with owner_db.begin() as conn:
            await conn.execute(text("ALTER TABLE onboarding_qa_messages DROP COLUMN IF EXISTS feedback_note"))
            await conn.execute(text("ALTER TABLE onboarding_qa_messages DROP COLUMN IF EXISTS text"))


async def _legacy_rows(org: AgentOrg, thread_id: uuid.UUID, n: int) -> list[uuid.UUID]:
    """Rows as P4 wrote them: plaintext (``text_sealed`` holds a placeholder the backfill overwrites)."""
    ids = sorted(uuid.uuid4() for _ in range(n))
    async with tenant_session(org.tenant_id) as db:
        cand = (await db.execute(text("SELECT candidate_id FROM onboarding_qa_threads WHERE id = :t"), {"t": thread_id})).scalar_one()
        for i, mid in enumerate(ids, start=100):
            await db.execute(
                text("INSERT INTO onboarding_qa_messages (id, tenant_id, thread_id, candidate_id, seq, role, text, feedback_note, text_sealed) VALUES (:id, :tid, :th, :c, :seq, 'asker', :tx, :fn, :ph)"),
                {"id": mid, "tid": org.tenant_id, "th": thread_id, "c": cand, "seq": i, "tx": f"Legacy question {i} about leave", "fn": "old note" if i == 100 else None, "ph": b"\x00"},
            )
    return ids


async def _plain(org: AgentOrg, ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    async with tenant_session(org.tenant_id) as db:
        rows = (await db.execute(text("SELECT id, text FROM onboarding_qa_messages WHERE id = ANY(:ids)"), {"ids": ids})).all()
    return {uuid.UUID(str(r[0])): r[1] for r in rows}


async def test_backfill_is_resumable_verified_and_idempotent(agent_org: AgentOrg, plaintext_columns: None) -> None:
    _, tid = await _asked(agent_org)
    ids = await _legacy_rows(agent_org, tid, 3)
    async with tenant_session(agent_org.tenant_id) as db:
        first = await sealing.backfill(db, agent_org.tenant_id, batch=1)
    assert (first.sealed, first.failed, first.last_id) == (1, 0, ids[0])
    async with tenant_session(agent_org.tenant_id) as db:
        rest = await sealing.backfill(db, agent_org.tenant_id, batch=10, resume_after=first.last_id)
    assert (rest.sealed, rest.failed) == (2, 0)
    assert set((await _plain(agent_org, ids)).values()) == {None}
    async with tenant_session(agent_org.tenant_id) as db:
        assert await sealing.open_message(db, agent_org.tenant_id, ids[0], (await db.execute(text("SELECT text_sealed FROM onboarding_qa_messages WHERE id = :i"), {"i": ids[0]})).scalar_one()) == "Legacy question 100 about leave"
        note = (await db.execute(text("SELECT feedback_note_sealed, topic_label, question_fingerprint FROM onboarding_qa_messages WHERE id = :i"), {"i": ids[0]})).one()
        assert await sealing.open_note(db, agent_org.tenant_id, ids[0], note[0]) == "old note"
        assert note[1] == "general: legacy question leave" and note[2]
        again = await sealing.backfill(db, agent_org.tenant_id)
        check = await sealing.verify(db, agent_org.tenant_id, recent=ids)
    assert (again.sealed, again.failed) == (0, 0)
    assert (check.plaintext_rows, check.undecryptable_rows) == (0, 0)


async def test_backfill_stops_on_failure_and_keeps_plaintext(agent_org: AgentOrg, plaintext_columns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tid = await _asked(agent_org)
    ids = await _legacy_rows(agent_org, tid, 3)
    real = vault_service.seal_field
    calls = {"n": 0}

    async def flaky(db: object, tenant_id: uuid.UUID, context: str, plaintext: str) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            return b"\x00\x01" + b"\x00" * 40  # a blob that will not round-trip
        return await real(db, tenant_id, context, plaintext)  # type: ignore[arg-type]

    monkeypatch.setattr(vault_service, "seal_field", flaky)
    async with tenant_session(agent_org.tenant_id) as db:
        out = await sealing.backfill(db, agent_org.tenant_id, batch=10)
        check = await sealing.verify(db, agent_org.tenant_id)
    assert out.failed == 1 and out.sealed == 0 and out.last_id is None
    plain = await _plain(agent_org, ids)
    assert all(v is not None for v in plain.values()), plain  # nothing NULLed for a row that did not round-trip
    assert check.plaintext_rows == 3


async def test_backfill_is_a_noop_after_the_drop(agent_org: AgentOrg) -> None:
    async with tenant_session(agent_org.tenant_id) as db:
        out = await sealing.backfill(db, agent_org.tenant_id)
        check = await sealing.verify(db, agent_org.tenant_id)
    assert (out.sealed, out.failed, check.plaintext_rows) == (0, 0, 0)
