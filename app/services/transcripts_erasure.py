# ruff: file-ignore[line-too-long]
"""Erasure handlers ``transcripts.records`` (order 63) and ``recordings.files`` (order 64), Epic 53 (FR-326, spec
0.15).

* ``transcripts.records`` -- transcripts of the candidate (merged profiles included): the segments are deleted,
  the uploaded source file is purged and the header stays as ``erased`` (audit ``transcript.erased``).
* ``recordings.files`` -- the recording files of ``voice_sessions.recording_file_id`` and
  ``interview_captures.file_id`` are purged and the FKs nulled; ``recording_allowed`` is unchanged. It claims the
  hub tables (keys, consent and status only besides the recording).

Rows in ``ctx.retained`` (``transcripts`` / ``voice_sessions`` / ``interview_captures``) are kept. Only candidate
subjects are handled.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interview_captures import InterviewCapture
from app.models.transcripts import Transcript, TranscriptSegment
from app.models.voice import VoiceSession
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler
from app.services.transcripts import retention
from app.services.transcripts.service import erase_rows


class TranscriptRecordsHandler:
    name = "transcripts.records"
    order = 63
    tables = ("transcripts", "transcript_segments")

    async def _rows(self, db: AsyncSession, ctx: ErasureContext) -> list[Transcript]:
        keep = ctx.retained.get("transcripts", set())
        ids = await subject_ids(db, ctx.candidate_id)
        rows = (await db.execute(select(Transcript).where(Transcript.candidate_id.in_(ids)))).scalars()
        return [t for t in rows if t.id not in keep]

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"transcripts": 0, "transcript_segments": 0}
        rows = await self._rows(db, ctx)
        deleted = await erase_rows(db, rows, reason=f"erasure:{ctx.reason}")
        return {"transcripts": len(rows), "transcript_segments": deleted}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        out: list[dict[str, Any]] = []
        rows = (
            await db.execute(select(Transcript).where(Transcript.candidate_id == ctx.candidate_id))
        ).scalars()
        for t in rows:
            segments = (
                await db.execute(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.transcript_id == t.id)
                    .order_by(TranscriptSegment.seq)
                )
            ).scalars()
            out.append(
                {
                    "source_kind": t.source_kind,
                    "status": t.status,
                    "created_at": t.created_at,
                    "segments": [
                        {
                            "speaker": s.speaker_role,
                            "start_ms": s.start_ms,
                            "end_ms": s.end_ms,
                            "text": s.text,
                        }
                        for s in segments
                    ],
                }
            )
        return {"transcripts": out}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        problems: list[str] = []
        for t in await self._rows(db, ctx):
            left = (
                await db.execute(
                    select(TranscriptSegment.id).where(TranscriptSegment.transcript_id == t.id).limit(1)
                )
            ).first()
            if left is not None or t.status != "erased":
                problems.append(f"transcript {t.id} still holds segments")
        return problems


class RecordingFilesHandler:
    name = "recordings.files"
    order = 64
    tables = ("voice_sessions", "interview_captures")

    async def _ids(self, db: AsyncSession, ctx: ErasureContext) -> tuple[list[Any], list[Any]]:
        ids = await subject_ids(db, ctx.candidate_id)
        keep_v = ctx.retained.get("voice_sessions", set())
        keep_c = ctx.retained.get("interview_captures", set())
        voice = [
            i
            for i in (
                await db.execute(
                    select(VoiceSession.id).where(
                        VoiceSession.candidate_id.in_(ids), VoiceSession.recording_file_id.is_not(None)
                    )
                )
            ).scalars()
            if i not in keep_v
        ]
        captures = [
            i
            for i in (
                await db.execute(
                    select(InterviewCapture.id).where(
                        InterviewCapture.candidate_id.in_(ids), InterviewCapture.file_id.is_not(None)
                    )
                )
            ).scalars()
            if i not in keep_c
        ]
        return voice, captures

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"voice_sessions": 0, "interview_captures": 0}
        voice, captures = await self._ids(db, ctx)
        return {
            "voice_sessions": await retention.purge_voice_recordings(db, voice),
            "interview_captures": await retention.purge_capture_recordings(db, captures),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # ruff: ignore[unused-method-argument]
        return None  # recordings are binary media: listed by ``files.candidate`` exports, not inlined

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        voice, captures = await self._ids(db, ctx)
        return [f"voice session {i} keeps a recording" for i in voice] + [
            f"interview capture {i} keeps a recording" for i in captures
        ]


register_erasure_handler(TranscriptRecordsHandler())
register_erasure_handler(RecordingFilesHandler())
