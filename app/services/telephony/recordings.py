# ruff: noqa: E501
"""Recordings: retrieval and provider deletion (Epic 76, story 76.4, spec A4).

* ``fetch`` (task ``telephony.fetch_recording``): only when the dial-time gate requested recording **and** the Recording
  Policy and ``call_recording`` consent still allow it (re-checked now). Withdrawn -> nothing is fetched; the provider
  copy is deleted. Otherwise the adapter downloads the recording (Egress Guard, 200 MB cap) into Tenant storage
  (``files_service.store_generated``: malware scan, encrypted at rest; owner ``voice_session``), the P4 session gets the
  file (``voice_service.on_provider_event('recording_stored')``) and the provider copy is deleted. Then STT is queued.
* ``delete_provider`` (task ``telephony.delete_provider_recording``): success -> ``deleted``; ``unsupported`` (Exotel)
  recorded once; failures retried hourly up to 72 h, then ``failed`` + ``telephony_provider_delete_failed`` + a Task.
* ``for_candidate``: ``ConsentWithdrawn(call_recording | voice_screening)`` or erasure -> every provider copy of the
  Candidate's calls is deleted (the P4 purge removes the stored file within its 24 h window).
"""

import uuid
from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.events import emit
from app.events.telephony import TelephonyProviderDeleteQueued
from app.models.connectors import ProviderConnectorConfig
from app.models.telephony import VoiceCall
from app.models.voice import VoiceSession
from app.services.connectors.voice.bridge import OP_DELETE_RECORDING
from app.services.telephony import common

DELETE_WINDOW = timedelta(hours=72)


async def _adapter(ctx: RequestContext, db: AsyncSession, call: VoiceCall) -> object:
    from app.services.connectors.registry import build_for

    cfg = await db.get(ProviderConnectorConfig, call.config_id) if call.config_id else None
    return await build_for(ctx, db, common.CATEGORY, cfg)


def _queue_delete(db: AsyncSession, call: VoiceCall) -> None:
    if call.provider_delete_status in ("deleted", "unsupported") or not call.provider_recording_ref:
        return
    call.provider_delete_status = "pending"
    call.provider_delete_first_at = call.provider_delete_first_at or common.now()
    emit(db, TelephonyProviderDeleteQueued(tenant_id=call.tenant_id, call_id=call.id))


async def still_allowed(ctx: RequestContext, db: AsyncSession, session: VoiceSession) -> bool:
    from app.services.telephony import gates

    record, _ = await gates.recording_decision(ctx, db, session, can_notice=True)
    return record


async def fetch(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID) -> str:
    """Retrieve one recording. Raises on provider errors (the task retries 5x, then ``mark_fetch_failed``)."""
    from app.services import files_service, voice_service

    call = (await db.execute(select(VoiceCall).where(VoiceCall.id == call_id).with_for_update())).scalar_one_or_none()
    if call is None or call.recording_fetch_status != "pending" or not call.provider_recording_ref:
        return call.recording_fetch_status if call is not None else "missing"
    session = await db.get(VoiceSession, call.voice_session_id)
    if not call.recording_requested or session is None or not await still_allowed(ctx, db, session):
        call.recording_fetch_status = "skipped"
        _queue_delete(db, call)
        await db.flush()
        audit.record("telephony.recording_skipped", "voice_call", call.id, session=db)
        return call.recording_fetch_status
    adapter = await _adapter(ctx, db, call)
    rec = await adapter.fetch_recording(call.provider_recording_ref)  # type: ignore[attr-defined]
    if rec is None or not rec.data:
        call.recording_fetch_status = "failed"
        await db.flush()
        return call.recording_fetch_status
    stored = await files_service.store_generated(
        ctx, db, rec.data, key=storage.new_key(call.tenant_id), filename=f"{call.human_id}.wav",
        content_type=rec.media_type or "audio/wav", owner_entity_type="voice_session", owner_entity_id=session.id,
    )  # fmt: skip
    await voice_service.on_provider_event(
        ctx, db, session.id, "recording_stored", recording_file_id=stored.id, recording_media_type=rec.media_type
    )
    call.recording_fetch_status = "stored"
    _queue_delete(db, call)
    await db.flush()
    audit.record("telephony.recording_stored", "voice_call", call.id, session=db)
    from app.services.telephony import stt_jobs

    queued = await stt_jobs.enqueue(
        ctx, db, source_kind="voice_call", source_id=call.id, file_id=stored.id, voice_session_id=session.id,
        candidate_id=call.candidate_id, channels=2 if call.dual_channel else 1,
    )  # fmt: skip
    if queued is None:
        await voice_service.on_provider_event(ctx, db, session.id, "transcribe_failed")
    return call.recording_fetch_status


async def mark_fetch_failed(db: AsyncSession, call_id: uuid.UUID) -> None:
    from app.services import voice_service

    call = await db.get(VoiceCall, call_id)
    if call is None or call.recording_fetch_status != "pending":
        return
    call.recording_fetch_status = "failed"
    await db.flush()
    await voice_service.on_provider_event(RequestContext(tenant_id=call.tenant_id, actor_kind="system"), db, call.voice_session_id, "transcribe_failed")


async def delete_provider(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID) -> str:
    call = (await db.execute(select(VoiceCall).where(VoiceCall.id == call_id).with_for_update())).scalar_one_or_none()
    if call is None or call.provider_delete_status not in ("pending", "failed") or not call.provider_recording_ref:
        return call.provider_delete_status if call is not None else "missing"
    call.provider_delete_attempts += 1
    call.provider_delete_first_at = call.provider_delete_first_at or common.now()
    try:
        adapter = await _adapter(ctx, db, call)
        result = await adapter.handle(OP_DELETE_RECORDING, {"recording_ref": call.provider_recording_ref})  # type: ignore[attr-defined]
        status = str(result.get("status") or "deleted")
        if status == "unsupported":
            call.provider_delete_status = "unsupported"
            audit.record("telephony.provider_delete_unsupported", "voice_call", call.id, session=db)
        else:
            call.provider_delete_status = "deleted"
            call.provider_deleted_at = common.now()
            call.provider_recording_ref = None
            audit.record("telephony.provider_recording_deleted", "voice_call", call.id, session=db)
    except Exception as exc:
        logger.info("Provider recording delete for {} failed: {}", call.id, type(exc).__name__)
        expired = common.now() - call.provider_delete_first_at >= DELETE_WINDOW
        if expired and call.provider_delete_status != "failed":
            call.provider_delete_status = "failed"
            await _delete_failed(db, call)
        elif not expired:
            call.provider_delete_status = "pending"
    await db.flush()
    return call.provider_delete_status


async def _delete_failed(db: AsyncSession, call: VoiceCall) -> None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    common.fan_out(call.tenant_id, "telephony_provider_delete_failed", {
        "event_id": f"telephony_provider_delete_failed:{call.id}", "call_id": str(call.id), "human_id": call.human_id,
    })  # fmt: skip
    data = TaskCreate.model_construct(
        title=f"Delete the recording of {call.human_id} in the provider console"[:200],
        notes="Talent OS could not delete the provider's copy of this call recording for 72 hours. Delete it in the provider console.",
        owner_id=call.placed_by, entity_type="application" if call.application_id else "candidate",
        entity_id=call.application_id or call.candidate_id, due_at=None,
        due_all_day=False, priority="high", reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            await tasks_service.create_system_task(RequestContext(tenant_id=call.tenant_id), db, data, source="agent")
    except Exception:  # pragma: no cover - the notification still goes out
        logger.exception("Provider delete Task for {} not created", call.id)


async def due_deletes(db: AsyncSession) -> list[uuid.UUID]:
    rows = (
        await db.execute(
            select(VoiceCall.id).where(
                VoiceCall.provider_delete_status == "pending", VoiceCall.provider_recording_ref.is_not(None)
            )
        )
    ).scalars()
    return list(rows)


async def for_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> list[uuid.UUID]:
    """Withdrawal / erasure: stop pending fetches and delete every provider copy of the Candidate's calls."""
    rows = list(
        (
            await db.execute(
                select(VoiceCall).where(VoiceCall.candidate_id == candidate_id, VoiceCall.provider_recording_ref.is_not(None)).with_for_update()
            )
        ).scalars()
    )
    ids: list[uuid.UUID] = []
    for call in rows:
        if call.recording_fetch_status == "pending":
            call.recording_fetch_status = "skipped"
        if call.provider_delete_status not in ("deleted", "unsupported"):
            _queue_delete(db, call)
            ids.append(call.id)
    await db.flush()
    return ids
