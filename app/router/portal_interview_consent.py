# ruff: file-ignore[line-too-long]
"""Candidate Portal: interview recording consent (Epic 60, A8): ``GET`` / ``POST /api/v1/portal/interviews/{id}/recording-consent``.

``Depends(current_candidate)`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations). Scoped to the signed-in
Candidate's own interviews (another Candidate's -> 404); 404 while no capture was requested for the interview."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from app.dependencies.candidate import current_candidate
from app.schemas.interview_capture_public import RecordingConsentIn, RecordingConsentView
from app.services import interview_captures_service as svc

router = APIRouter(prefix="/portal", tags=["portal"])
Candidate = Annotated[Any, Depends(current_candidate)]
NO_STORE = {"Cache-Control": "no-store"}


def _candidate_id(principal: Any) -> uuid.UUID:
    return getattr(principal, "candidate_id", None) or principal.id


@router.get("/interviews/{interview_id}/recording-consent", response_model=RecordingConsentView)
async def view(interview_id: uuid.UUID, principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return await svc.portal_view(principal.tenant_id, _candidate_id(principal), interview_id)


@router.post("/interviews/{interview_id}/recording-consent", response_model=RecordingConsentView)
async def decide(
    interview_id: uuid.UUID, body: RecordingConsentIn, principal: Candidate, response: Response
) -> Any:
    response.headers.update(NO_STORE)
    return await svc.portal_decide(
        principal.tenant_id, _candidate_id(principal), interview_id, body.decision, body.country, body.region
    )
