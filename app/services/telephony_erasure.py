# ruff: noqa: E501
"""Telephony erasure handler ``telephony.calls`` (order 71), Epic 76 (spec A10, FR-395).

On Candidate erasure (merged ids included): the Candidate's ``stt_jobs`` rows are deleted; ``voice_calls`` keep their
metadata (cost / audit) with ``candidate_id`` and ``external_ref`` nulled; a provider recording copy not yet deleted
stays referenced (``provider_recording_ref`` is a provider id, not personal data) and is queued for deletion at the
provider (``telephony.withdraw``); other refs are nulled. Stored recordings are purged by ``recordings.files`` and
transcripts by ``transcripts.records``. Recruiter phones (``user_call_phones``) are deleted with the user (FK cascade).
A top-level module so ``privacy`` never imports the Phase 5 package.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telephony import SttJob, VoiceCall
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class TelephonyCallsHandler:
    name = "telephony.calls"
    order = 71
    tables = ("voice_calls", "stt_jobs")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"calls": 0, "stt_jobs": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        jobs = await db.execute(delete(SttJob).where(SttJob.candidate_id.in_(ids)))
        calls = list((await db.execute(select(VoiceCall).where(VoiceCall.candidate_id.in_(ids)))).scalars())
        pending = []
        for call in calls:
            keep_ref = call.provider_recording_ref is not None and call.provider_delete_status not in ("deleted", "unsupported")
            if keep_ref:
                call.provider_delete_status = "pending"
                pending.append(call.id)
            else:
                call.provider_recording_ref = None
            if call.recording_fetch_status == "pending":
                call.recording_fetch_status = "skipped"
        if calls:
            await db.execute(
                update(VoiceCall).where(VoiceCall.candidate_id.in_(ids)).values(candidate_id=None, external_ref=None)
            )
        await db.flush()
        if pending:
            from app.core.events import emit
            from app.events.telephony import TelephonyProviderDeleteQueued

            for call_id in pending:
                emit(db, TelephonyProviderDeleteQueued(tenant_id=ctx.tenant_id, call_id=call_id))
        return {"calls": len(calls), "stt_jobs": int(getattr(jobs, "rowcount", 0) or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (await db.execute(select(VoiceCall).where(VoiceCall.candidate_id == ctx.candidate_id))).scalars()
        items = [
            {"call": r.human_id, "status": r.status, "requested_at": r.requested_at, "duration_s": r.duration_s, "recorded": r.recording_requested}
            for r in rows
        ]
        return {"voice_calls": items} if items else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        out = []
        if (await db.execute(select(VoiceCall.id).where(VoiceCall.candidate_id.in_(ids)).limit(1))).first() is not None:
            out.append("voice calls still reference the candidate")
        if (await db.execute(select(SttJob.id).where(SttJob.candidate_id.in_(ids)).limit(1))).first() is not None:
            out.append("speech-to-text jobs remain")
        return out


register_erasure_handler(TelephonyCallsHandler())
