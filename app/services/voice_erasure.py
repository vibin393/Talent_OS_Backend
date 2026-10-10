# ruff: noqa: E501
"""Voice Screening erasure handler ``voice.sessions`` (order 67), Epic 58 (FR-326, AD-34).

On Candidate erasure (merged ids included): the AI summary / gaps, the confirmed location and the Fake script ref of
the Candidate's voice sessions are nulled and the upload attestations deleted. Recording files are purged by
``recordings.files`` (order 64) and transcripts by ``transcripts.records`` (order 63); the hub row keeps keys and
status. A top-level module so ``privacy`` never imports the Phase 4 domain.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.voice import VoiceSession, VoiceUploadAttestation
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class VoiceSessionsHandler:
    name = "voice.sessions"
    order = 67
    tables = ("voice_upload_attestations",)

    async def _ids(self, db: AsyncSession, ctx: ErasureContext) -> list[Any]:
        ids = await subject_ids(db, ctx.candidate_id)
        keep = ctx.retained.get("voice_sessions", set())
        rows = (await db.execute(select(VoiceSession.id).where(VoiceSession.candidate_id.in_(ids)))).scalars()
        return [r for r in rows if r not in keep]

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"sessions": 0, "attestations": 0}
        ids = await self._ids(db, ctx)
        if not ids:
            return {"sessions": 0, "attestations": 0}
        att = await db.execute(
            delete(VoiceUploadAttestation).where(VoiceUploadAttestation.voice_session_id.in_(ids))
        )
        await db.execute(
            update(VoiceSession)
            .where(VoiceSession.id.in_(ids))
            .values(summary=None, gaps=[], confirmed_country=None, confirmed_region=None, script_ref=None)
        )
        await db.flush()
        return {"sessions": len(ids), "attestations": int(getattr(att, "rowcount", 0) or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (
            await db.execute(select(VoiceSession).where(VoiceSession.candidate_id == ctx.candidate_id))
        ).scalars()
        items = [
            {
                "voice_session": r.human_id,
                "status": r.status,
                "created_at": r.created_at,
                "summary": r.summary,
                "gaps": r.gaps,
                "ai_generated": True,
            }
            for r in rows
        ]
        return {"voice_sessions": items} if items else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await self._ids(db, ctx)
        if not ids:
            return []
        left = (
            await db.execute(
                select(VoiceSession.id)
                .where(VoiceSession.id.in_(ids), VoiceSession.summary.is_not(None))
                .limit(1)
            )
        ).first()
        att = (
            await db.execute(
                select(VoiceUploadAttestation.id)
                .where(VoiceUploadAttestation.voice_session_id.in_(ids))
                .limit(1)
            )
        ).first()
        out = []
        if left is not None:
            out.append("voice session summaries remain")
        if att is not None:
            out.append("voice upload attestations remain")
        return out


register_erasure_handler(VoiceSessionsHandler())
