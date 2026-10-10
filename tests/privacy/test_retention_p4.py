# ruff: noqa: E501
"""Epic 53 (spec 0.3 / 0.15): Phase 4 retention classes and bindings for transcripts / recordings, and the erasure
handlers ``transcripts.records`` (63) and ``recordings.files`` (64)."""

import uuid

from sqlalchemy import func, select

from app.core import storage
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.sequences import next_human_id
from app.core.permissions import CATALOG, Principal, Scope
from app.models.privacy import RetentionClass
from app.models.transcripts import Transcript, TranscriptSegment
from app.models.voice import VoiceSession
from app.schemas.transcripts import TranscriptSegmentIn
from app.services import files_service, transcripts_service
from app.services.privacy import registry
from app.services.privacy.registry import ErasureContext
from app.services.privacy.retention_classes import P4_DEFAULT_CLASSES, retention_bindings, seed_defaults
from app.services.transcripts import retention
from tests.epic12 import Org3, create_candidate


def ctx(org: Org3) -> RequestContext:
    principal = Principal(
        type="user",
        id=org.admin.user_id,
        tenant_id=org.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def _setup(org: Org3) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a candidate with a ready transcript and a voice session holding a recording."""
    cand = uuid.UUID((await create_candidate(org.admin))["id"])
    c = ctx(org)
    async with tenant_session(org.tenant_id) as db:
        t = await transcripts_service.create(c, db, source_kind="voice_session", candidate_id=cand)
        await transcripts_service.append_segments(
            c, db, t.id, [TranscriptSegmentIn(speaker_role="candidate", text="My PAN is secret")], final=True
        )
        rec = await files_service.store_generated(
            c, db, b"RIFF0000WAVE", key=storage.new_key(org.tenant_id), filename="call.wav",
            content_type="audio/wav", owner_entity_type="candidate", owner_entity_id=cand,
        )  # fmt: skip
        vs = VoiceSession(
            tenant_id=org.tenant_id, candidate_id=cand, mode="call", provider_key="fake", recording_allowed=True,
            recording_file_id=rec.id, transcript_id=t.id,
            human_id=await next_human_id(db, "voice_session", "VS"), owner_user_id=org.admin.user_id,
        )  # fmt: skip
        db.add(vs)
        await db.flush()
        return cand, t.id, vs.id, rec.id


async def test_p4_classes_seeded_and_bindings_registered(org3: Org3) -> None:
    async with tenant_session(org3.tenant_id) as db:
        await seed_defaults(db)
        await seed_defaults(db)  # idempotent
        rows = {
            r.key: r
            for r in (
                await db.execute(
                    select(RetentionClass).where(RetentionClass.key.in_(list(P4_DEFAULT_CLASSES)))
                )
            ).scalars()
        }
    assert set(rows) == {"agent_payloads", "agent_logs", "conversation_records", "recordings"}
    assert (
        rows["conversation_records"].min_retention_months,
        rows["conversation_records"].expiry_action,
    ) == (6, "delete")
    assert (rows["recordings"].min_retention_months, rows["recordings"].expiry_action) == (3, "delete")
    assert (rows["agent_logs"].min_retention_months, rows["agent_logs"].expiry_action) == (12, "anonymize")
    bindings = retention_bindings()
    assert bindings["transcript_segments"].class_key == "conversation_records"
    assert bindings["voice_sessions"].class_key == "recordings"
    assert bindings["interview_captures"].class_key == "recordings"


async def test_expiry_functions(org3: Org3) -> None:
    cand, tid, vsid, rec = await _setup(org3)
    async with tenant_session(org3.tenant_id) as db:
        seg_ids = list(
            (
                await db.execute(select(TranscriptSegment.id).where(TranscriptSegment.transcript_id == tid))
            ).scalars()
        )
        assert await retention.segment_subject(db, seg_ids[0]) == cand
        assert await retention.segment_anchor(db, seg_ids[0]) is not None
        assert await retention.voice_subject(db, vsid) == cand
        assert (await retention.expire_segments(db, seg_ids, "delete")) == {"deleted": 1}
        assert (await retention.expire_voice_recordings(db, [vsid], "delete")) == {"deleted": 1}
    async with tenant_session(org3.tenant_id) as db:
        t = await db.get(Transcript, tid)
        assert t is not None and (t.status, t.segment_count) == ("erased", 0)
        vs = await db.get(VoiceSession, vsid)
        assert vs is not None and vs.recording_file_id is None and vs.recording_allowed is True
        f = await files_service.get_file(db, rec)
        assert f is not None and f.deleted_at is not None


async def test_erasure_handlers(org3: Org3) -> None:
    cand, tid, vsid, rec = await _setup(org3)
    handlers = {h.name: h for h in registry.handlers()}
    tx, recs = handlers["transcripts.records"], handlers["recordings.files"]
    assert (tx.order, recs.order) == (63, 64)
    assert {"transcripts", "transcript_segments", "voice_sessions", "interview_captures"} <= set(
        registry.claimed_tables()
    )
    ectx = ErasureContext(tenant_id=org3.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(org3.tenant_id) as db:
        exported = await tx.export(db, ectx)
        assert (
            exported is not None and exported["transcripts"][0]["segments"][0]["text"] == "My PAN is secret"
        )
        assert await tx.verify(db, ectx) != [] and await recs.verify(db, ectx) != []
        assert await tx.erase(db, ectx) == {"transcripts": 1, "transcript_segments": 1}
        assert await recs.erase(db, ectx) == {"voice_sessions": 1, "interview_captures": 0}
    async with tenant_session(org3.tenant_id) as db:
        assert await tx.verify(db, ectx) == [] and await recs.verify(db, ectx) == []
        left = (
            await db.execute(select(func.count()).where(TranscriptSegment.transcript_id == tid))
        ).scalar_one()
        assert left == 0
        assert (await db.get(Transcript, tid)).status == "erased"  # type: ignore[union-attr]
        assert (await db.get(VoiceSession, vsid)).recording_file_id is None  # type: ignore[union-attr]
        assert (await files_service.get_file(db, rec)).deleted_at is not None  # type: ignore[union-attr]
    other = ErasureContext(
        tenant_id=org3.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr", subject_kind="contact"
    )
    async with tenant_session(org3.tenant_id) as db:
        assert await tx.erase(db, other) == {"transcripts": 0, "transcript_segments": 0}
