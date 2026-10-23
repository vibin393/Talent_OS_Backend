# ruff: file-ignore[line-too-long]
"""Interview Assistant API (Epic 60, spec A8).

| Method + path | Permission |
|---|---|
| ``GET /interviews/{id}/capture`` | ``interview:read`` or ``interview_capture:manage`` (+ the P1 read rule) |
| ``POST /interviews/{id}/capture`` · ``POST /interviews/{id}/capture/consent-requests`` | ``interview_capture:manage`` (participant or ``interview:schedule``) |
| ``POST /interviews/{id}/capture/transcript`` · ``POST /interviews/{id}/capture/recording`` (multipart + ``attest``) | ``interview_capture:manage`` (participant) |
| ``POST /interviews/{id}/capture/speakers`` ``{candidate_speaker}`` | ``interview_capture:manage`` |
| ``GET /interviews/{id}/capture/outputs`` · ``GET /interviews/{id}/scorecard-draft`` · ``POST …/scorecard-draft/opened`` · ``POST …/scorecard-draft/dismiss`` | participant (visibility rules; others 404) |
| ``POST /interviews/{id}/capture/rerun`` (human only) | ``interview_capture:manage`` |
| ``GET`` / ``POST`` / ``DELETE /me/interview-recording-ack`` | signed-in user |
| ``GET`` / ``PUT /settings/agents/interview-assistant`` | ``ai:agent_configure`` (PUT human only) |
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.permissions import Principal, current_principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.interview_captures import (
    AssistantSettingsIn,
    AssistantSettingsOut,
    CaptureStatusOut,
    ConsentRequestsOut,
    DraftOut,
    OutputsOut,
    RecordingAckOut,
    RerunOut,
    SpeakerIn,
    UploadOut,
)
from app.services import interview_captures_service as svc

router = APIRouter(tags=["interview-assistant"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require_any("interview:read", "interview_capture:manage"))]
Manage = Annotated[Principal, Depends(require("interview_capture:manage"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Signed = Annotated[Principal, Depends(current_principal)]
MAX_READ_MB = 200


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


def _user(principal: Principal) -> None:
    if principal.type != "user":
        raise Forbidden("Only a signed-in user can do this", code="human_only_action")


@router.get("/interviews/{interview_id}/capture", response_model=CaptureStatusOut)
async def capture_status(interview_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.capture_status(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/capture", response_model=ConsentRequestsOut)
async def request_capture(interview_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.request_capture(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/capture/consent-requests", response_model=ConsentRequestsOut)
async def consent_requests(interview_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.request_acks(get_context(), db, interview_id)


async def _read(upload: UploadFile, limit_mb: int) -> bytes:
    from app.services import files_service

    return await files_service.read_upload(upload, limit_mb)


async def _upload_out(db: Any, interview_id: uuid.UUID, result: dict[str, Any]) -> UploadOut:
    status = await svc.capture_status(get_context(), db, interview_id)
    return UploadOut(
        status=status,
        transcript_id=result.get("transcript_id"),
        needs_transcript=bool(result.get("needs_transcript")),
    )


@router.post("/interviews/{interview_id}/capture/transcript", response_model=UploadOut)
async def upload_transcript(
    interview_id: uuid.UUID,
    _: Manage,
    db: DB,
    file: Annotated[UploadFile, File()],
    attest: Annotated[bool, Form()] = False,
) -> Any:
    _human()
    data = await _read(file, 6)
    result = await svc.upload_transcript(
        get_context(), db, interview_id, filename=file.filename, data=data, attest=attest
    )
    return await _upload_out(db, interview_id, result)


@router.post("/interviews/{interview_id}/capture/recording", response_model=UploadOut)
async def upload_recording(
    interview_id: uuid.UUID,
    _: Manage,
    db: DB,
    file: Annotated[UploadFile, File()],
    attest: Annotated[bool, Form()] = False,
) -> Any:
    _human()
    if (file.content_type or "").lower().startswith("video/"):
        from app.core.errors import ValidationFailed

        raise ValidationFailed(
            "Video files aren't accepted. Only the words are used.", code="unsupported_media"
        )
    data = await _read(file, MAX_READ_MB)
    result = await svc.upload_recording(
        get_context(),
        db,
        interview_id,
        filename=file.filename,
        data=data,
        content_type=file.content_type,
        attest=attest,
    )
    return await _upload_out(db, interview_id, result)


@router.post("/interviews/{interview_id}/capture/speakers", response_model=CaptureStatusOut)
async def speakers(interview_id: uuid.UUID, body: SpeakerIn, _: Manage, db: DB) -> Any:
    _human()
    await svc.relabel(get_context(), db, interview_id, body.candidate_speaker)
    return await svc.capture_status(get_context(), db, interview_id)


@router.get("/interviews/{interview_id}/capture/outputs", response_model=OutputsOut)
async def outputs(interview_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_outputs(get_context(), db, interview_id)


@router.get("/interviews/{interview_id}/scorecard-draft", response_model=DraftOut)
async def my_draft(interview_id: uuid.UUID, principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.my_draft(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/scorecard-draft/opened", response_model=DraftOut)
async def draft_opened(interview_id: uuid.UUID, principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.mark_draft_opened(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/scorecard-draft/dismiss", response_model=DraftOut)
async def draft_dismiss(interview_id: uuid.UUID, principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.dismiss_draft(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/capture/rerun", response_model=RerunOut)
async def rerun(interview_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.rerun(get_context(), db, interview_id)


@router.get("/me/interview-recording-ack", response_model=RecordingAckOut)
async def get_ack(principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.my_ack(get_context(), db)


@router.post("/me/interview-recording-ack", response_model=RecordingAckOut)
async def give_ack(principal: Signed, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.give_ack(get_context(), db)


@router.delete("/me/interview-recording-ack", response_model=RecordingAckOut)
async def revoke_ack(principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.revoke_ack(get_context(), db)


@router.get("/settings/agents/interview-assistant", response_model=AssistantSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings/agents/interview-assistant", response_model=AssistantSettingsOut)
async def put_settings(body: AssistantSettingsIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.update_settings(get_context(), db, body)
