# ruff: noqa: E501
"""Retention of conversation records and recordings (Epic 53, spec 0.15).

* ``conversation_records`` (6 months, delete) <- ``transcript_segments``: expiry deletes the segments and marks
  transcripts left without segments ``erased`` (the header stays).
* ``recordings`` (3 months, delete) <- ``voice_sessions`` / ``interview_captures``: expiry deletes the recording
  file (``files_service.purge_file``) and nulls the FK; the hub row and ``recording_allowed`` stay.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interview_captures import InterviewCapture
from app.models.transcripts import Transcript, TranscriptSegment
from app.models.voice import VoiceSession
from app.services import files_service


async def expire_segments(
    db: AsyncSession,
    ids: Sequence[uuid.UUID],
    action: str = "delete",  # noqa: ARG001
) -> dict[str, int]:
    ids = list(ids)
    if not ids:
        return {"deleted": 0}
    parents = set(
        (
            await db.execute(select(TranscriptSegment.transcript_id).where(TranscriptSegment.id.in_(ids)))
        ).scalars()
    )
    result = await db.execute(delete(TranscriptSegment).where(TranscriptSegment.id.in_(ids)))
    now = datetime.now(UTC)
    for tid in parents:
        row = await db.get(Transcript, tid)
        if row is None:
            continue
        left = (
            await db.execute(select(func.count()).where(TranscriptSegment.transcript_id == tid))
        ).scalar_one()
        row.segment_count = int(left)
        if not left and row.status != "erased":
            row.status, row.erased_at = "erased", now
            if row.file_id is not None:
                await files_service.purge_file(db, row.file_id)
    await db.flush()
    return {"deleted": int(getattr(result, "rowcount", 0) or 0)}


async def purge_voice_recordings(db: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    rows = (
        await db.execute(
            select(VoiceSession).where(
                VoiceSession.id.in_(list(ids)), VoiceSession.recording_file_id.is_not(None)
            )
        )
    ).scalars()
    n = 0
    for row in rows:
        await files_service.purge_file(db, row.recording_file_id)  # type: ignore[arg-type]
        row.recording_file_id = None
        n += 1
    await db.flush()
    return n


async def purge_capture_recordings(db: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    rows = (
        await db.execute(
            select(InterviewCapture).where(
                InterviewCapture.id.in_(list(ids)), InterviewCapture.file_id.is_not(None)
            )
        )
    ).scalars()
    n = 0
    for row in rows:
        await files_service.purge_file(db, row.file_id)  # type: ignore[arg-type]
        row.file_id = None
        n += 1
    await db.flush()
    return n


async def expire_voice_recordings(
    db: AsyncSession,
    ids: Sequence[uuid.UUID],
    action: str = "delete",  # noqa: ARG001
) -> dict[str, int]:
    return {"deleted": await purge_voice_recordings(db, ids)}


async def expire_capture_recordings(
    db: AsyncSession,
    ids: Sequence[uuid.UUID],
    action: str = "delete",  # noqa: ARG001
) -> dict[str, int]:
    return {"deleted": await purge_capture_recordings(db, ids)}


# ---------------------------------------------------------------- resolvers (row id -> subject / anchor)
async def segment_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Transcript.candidate_id)
            .join(TranscriptSegment, TranscriptSegment.transcript_id == Transcript.id)
            .where(TranscriptSegment.id == row_id)
        )
    ).scalar_one_or_none()


async def segment_anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    return (
        await db.execute(select(TranscriptSegment.created_at).where(TranscriptSegment.id == row_id))
    ).scalar_one_or_none()


async def voice_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
    return (
        await db.execute(select(VoiceSession.candidate_id).where(VoiceSession.id == row_id))
    ).scalar_one_or_none()


async def voice_anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    return (
        await db.execute(select(VoiceSession.created_at).where(VoiceSession.id == row_id))
    ).scalar_one_or_none()


async def capture_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
    return (
        await db.execute(select(InterviewCapture.candidate_id).where(InterviewCapture.id == row_id))
    ).scalar_one_or_none()


async def capture_anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    return (
        await db.execute(select(InterviewCapture.created_at).where(InterviewCapture.id == row_id))
    ).scalar_one_or_none()


__all__ = [
    "expire_capture_recordings",
    "expire_segments",
    "expire_voice_recordings",
    "purge_capture_recordings",
    "purge_voice_recordings",
]
