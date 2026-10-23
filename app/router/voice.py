# ruff: file-ignore[line-too-long]
"""Voice Screening API (Epic 58, Part C).

| Method + path | Permission |
|---|---|
| ``POST /applications/{id}/voice-sessions`` ``{mode, provider_key?, script_ref?}`` + Idempotency-Key (human) | ``voice:upload`` |
| ``GET /applications/{id}/voice-sessions`` · ``GET /voice-sessions/{id}`` | ``application:read`` |
| ``POST /voice-sessions/{id}/transcript`` · ``POST /voice-sessions/{id}/recording`` (multipart + attestation) | ``voice:upload`` |
| ``GET /voice-sessions/{id}/transcript`` · ``POST /voice-sessions/{id}/speakers`` · ``GET /voice-sessions/{id}/playback`` | ``screening:conduct`` |
| ``POST /voice-sessions/{id}/cancel`` | ``voice:upload`` |
| ``POST /voice-sessions/{id}/complete-review`` (human) | ``screening:conduct`` |
| ``GET`` / ``PUT /settings/agents/voice-screening`` | ``ai:agent_configure`` (PUT human only) |

The review itself reuses Epic 57's ``/screening-reviews?voice_session_id=`` endpoints; the outcome is set through the
unchanged P1 screening outcome endpoint (★).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.voice import (
    PlaybackOut,
    SpeakerLabelIn,
    VoiceSessionOut,
    VoiceSessionRequestIn,
    VoiceSettingsIn,
    VoiceSettingsOut,
    VoiceTranscriptOut,
)
from app.services import voice_service as svc
from app.services.voice.common import TooLarge

router = APIRouter(tags=["voice-screening"])
DB = TenantDBSessionDep
Upload = Annotated[Principal, Depends(require("voice:upload"))]
Read = Annotated[Principal, Depends(require("application:read"))]
Conduct = Annotated[Principal, Depends(require("screening:conduct"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
MAX_RECORDING_MB = 200
MAX_TRANSCRIPT_MB = 5


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


async def _read(upload: UploadFile, limit_mb: int) -> bytes:
    data: bytes = await upload.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        raise TooLarge(f"The file is larger than {limit_mb} MB", details={"max_mb": limit_mb})
    return data


@router.post(
    "/applications/{application_id}/voice-sessions",
    response_model=VoiceSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_session(
    application_id: uuid.UUID,
    body: VoiceSessionRequestIn,
    _: Upload,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    _human()
    ctx = get_context()

    async def _run() -> VoiceSessionOut:
        return await svc.request(
            ctx,
            db,
            application_id,
            mode=body.mode,
            provider_key=body.provider_key,
            script_ref=body.script_ref,
        )

    return await idem.run(_run)


@router.get("/applications/{application_id}/voice-sessions", response_model=list[VoiceSessionOut])
async def list_sessions(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_for_application(get_context(), db, application_id)


@router.get("/voice-sessions/{session_id}", response_model=VoiceSessionOut)
async def get_session(session_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get(get_context(), db, session_id)


@router.post("/voice-sessions/{session_id}/transcript", response_model=VoiceSessionOut)
async def upload_transcript(
    session_id: uuid.UUID,
    _: Upload,
    db: DB,
    file: Annotated[UploadFile, File()],
    consent_obtained: Annotated[bool, Form()] = False,
    policy_followed: Annotated[bool, Form()] = False,
) -> Any:
    _human()
    data = await _read(file, MAX_TRANSCRIPT_MB)
    return await svc.upload_transcript(
        get_context(),
        db,
        session_id,
        data,
        file.filename,
        consent_obtained=consent_obtained,
        policy_followed=policy_followed,
    )


@router.post("/voice-sessions/{session_id}/recording", response_model=VoiceSessionOut)
async def upload_recording(
    session_id: uuid.UUID,
    _: Upload,
    db: DB,
    file: Annotated[UploadFile, File()],
    consent_obtained: Annotated[bool, Form()] = False,
    policy_followed: Annotated[bool, Form()] = False,
) -> Any:
    _human()
    if (file.content_type or "").lower().startswith("video/"):
        from app.core.errors import ValidationFailed

        raise ValidationFailed(
            "Video files aren't accepted. Upload an audio recording.", code="unsupported_media"
        )
    data = await _read(file, MAX_RECORDING_MB)
    return await svc.upload_recording(
        get_context(), db, session_id, data, file.filename, file.content_type,
        consent_obtained=consent_obtained, policy_followed=policy_followed,
    )  # fmt: skip


@router.get("/voice-sessions/{session_id}/transcript", response_model=VoiceTranscriptOut)
async def get_transcript(session_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.transcript(get_context(), db, session_id)


@router.post("/voice-sessions/{session_id}/speakers", response_model=VoiceSessionOut)
async def label_speakers(session_id: uuid.UUID, body: SpeakerLabelIn, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.label_speakers(get_context(), db, session_id, body.segment_seqs_as_candidate)


@router.get("/voice-sessions/{session_id}/playback", response_model=PlaybackOut)
async def playback(session_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.playback_url(get_context(), db, session_id)


@router.post("/voice-sessions/{session_id}/cancel", response_model=VoiceSessionOut)
async def cancel(session_id: uuid.UUID, _: Upload, db: DB) -> Any:
    _human()
    return await svc.cancel(get_context(), db, session_id)


@router.post("/voice-sessions/{session_id}/complete-review", response_model=VoiceSessionOut)
async def complete_review(session_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.complete_review(get_context(), db, session_id)


@router.get("/settings/agents/voice-screening", response_model=VoiceSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings/agents/voice-screening", response_model=VoiceSettingsOut)
async def put_settings(body: VoiceSettingsIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.update_settings(get_context(), db, body)
