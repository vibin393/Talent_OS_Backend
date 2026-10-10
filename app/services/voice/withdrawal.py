# ruff: noqa: E501
"""Withdrawal, sweep and retention of voice sessions (Epic 58, A5; FR-294, FR-326).

* ``ConsentWithdrawn(call_recording)`` -> task ``voice.purge_recordings`` (immediately): every recording of the
  Candidate is purged from tenant storage, ``recording_file_id`` NULL, ``recording_allowed`` false,
  ``recording_deleted_at`` / ``withdrawn_at`` set; audited.
* ``ConsentWithdrawn(voice_screening)`` -> as above, plus the voice transcripts erased, the voice answers deleted
  (Epic 57 ``erase_voice_answers``), any active run cancelled and the session ``cancelled`` unless reviewed.
* The hourly sweep re-checks: any stored recording without an active ``call_recording`` consent (or any transcript
  without ``voice_screening`` consent) is purged -- the 24 h SLA if a task failed.
* Retention (``conversation_records``): ``summary`` / ``gaps`` nulled.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.voice import VoiceSession
from app.services.voice import common


async def purge_recording(db: AsyncSession, row: VoiceSession, *, reason: str) -> bool:
    from app.services import files_service

    if row.recording_file_id is None:
        return False
    await files_service.purge_file(db, row.recording_file_id)
    row.recording_file_id = None
    row.recording_allowed = False
    row.recording_deleted_at = common.now()
    await db.flush()
    audit.record(
        "voice_session.recording_deleted",
        "voice_session",
        row.id,
        session=db,
        actor_type="system",
        actor_id=None,
        reason=reason,
    )
    return True


async def _erase_voice(db: AsyncSession, row: VoiceSession, *, reason: str) -> None:
    from app.models.transcripts import Transcript
    from app.services import screening_agent_service
    from app.services.transcripts.service import erase_rows
    from app.services.voice import sessions

    rows = list(
        (
            await db.execute(
                select(Transcript).where(
                    Transcript.source_kind == "voice_session", Transcript.source_id == row.id
                )
            )
        ).scalars()
    )
    if rows:
        await erase_rows(db, rows, reason=f"withdrawal:{reason}")
    await screening_agent_service.erase_voice_answers(
        RequestContext(tenant_id=row.tenant_id, actor_kind="system"), db, row.id
    )
    row.summary = None
    row.gaps = []
    if row.status != "reviewed":
        row.status = "cancelled"
    await db.flush()
    await sessions.cancel_active_run(db, row, reason="The candidate withdrew consent")
    audit.record(
        "voice_session.withdrawn",
        "voice_session",
        row.id,
        session=db,
        actor_type="system",
        actor_id=None,
        reason=reason,
    )


async def for_candidate(
    db: AsyncSession, candidate_id: uuid.UUID, purpose: str, *, reason: str = "consent_withdrawn"
) -> int:
    rows = list(
        (
            await db.execute(
                select(VoiceSession).where(VoiceSession.candidate_id == candidate_id).with_for_update()
            )
        ).scalars()
    )
    n = 0
    for row in rows:
        touched = await purge_recording(db, row, reason=reason)
        if purpose == common.PURPOSE_CALL and (
            row.transcript_id is not None or row.status in common.ACTIVE_FOR_WITHDRAWAL
        ):
            await _erase_voice(db, row, reason=reason)
            touched = True
        if touched:
            row.withdrawn_at = common.now()
            n += 1
    await db.flush()
    return n


async def sweep(db: AsyncSession) -> int:
    """Hourly: recordings / transcripts whose consent is gone are removed (24 h SLA)."""
    n = 0
    rows = list(
        (
            await db.execute(
                select(VoiceSession).where(
                    (VoiceSession.recording_file_id.is_not(None)) | (VoiceSession.transcript_id.is_not(None))
                )
            )
        ).scalars()
    )
    for row in rows:
        if row.recording_file_id is not None and not await common.has_consent(
            db, row.candidate_id, common.PURPOSE_RECORDING
        ):
            n += await for_candidate(db, row.candidate_id, common.PURPOSE_RECORDING, reason="sweep")
        if (
            row.status != "cancelled"
            and row.withdrawn_at is None
            and row.transcript_id is not None
            and not await common.has_consent(db, row.candidate_id, common.PURPOSE_CALL)
        ):
            n += await for_candidate(db, row.candidate_id, common.PURPOSE_CALL, reason="sweep")
    return n


# ================================================================ retention (conversation_records)
async def expire_summaries(
    db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete"  # noqa: ARG001
) -> dict[str, int]:
    ids = list(ids)
    for sid in ids:
        row = await db.get(VoiceSession, sid)
        if row is not None:
            row.summary = None
            row.gaps = []
    await db.flush()
    return {"deleted": len(ids)}


async def session_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
    return (
        await db.execute(select(VoiceSession.candidate_id).where(VoiceSession.id == row_id))
    ).scalar_one_or_none()


async def session_anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    return (
        await db.execute(select(VoiceSession.created_at).where(VoiceSession.id == row_id))
    ).scalar_one_or_none()


__all__ = [
    "expire_summaries",
    "for_candidate",
    "purge_recording",
    "session_anchor",
    "session_subject",
    "sweep",
]
