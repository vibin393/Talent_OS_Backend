"""Epic 53 (spec 0.3, AD-67): Phase 4 consent purposes and sources exist and the DB CHECKs accept them."""

import uuid

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.privacy import CONSENT_PURPOSES, CONSENT_SOURCES, Consent
from app.services.privacy import consent
from tests.epic12 import Org3, create_candidate

P4_PURPOSES = (
    "ai_agent_communication",
    "voice_screening",
    "call_recording",
    "interview_recording",
    "knowledge_assistant",
)
P4_SOURCES = ("conversation", "voice_consent_link", "interview_invite")


def test_constants_widened() -> None:
    assert set(P4_PURPOSES) <= set(CONSENT_PURPOSES)
    assert set(P4_SOURCES) <= set(CONSENT_SOURCES)


async def test_call_recording_consent_from_conversation_inserts(org3: Org3) -> None:
    cid = uuid.UUID((await create_candidate(org3.admin))["id"])
    ctx = RequestContext(tenant_id=org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        row = await consent.record(db, cid, "call_recording", "conversation", None, ctx)
        assert (row.purpose, row.source, row.status) == ("call_recording", "conversation", "granted")
        assert await consent.has_active_consent(db, cid, "call_recording")
        for purpose, source in (
            ("voice_screening", "voice_consent_link"),
            ("interview_recording", "interview_invite"),
            ("ai_agent_communication", "conversation"),
            ("knowledge_assistant", "conversation"),
        ):
            await consent.record(db, cid, purpose, source, None, ctx)
    async with tenant_session(org3.tenant_id) as db:
        purposes = set(
            (await db.execute(select(Consent.purpose).where(Consent.candidate_id == cid))).scalars()
        )
        assert set(P4_PURPOSES) <= purposes
