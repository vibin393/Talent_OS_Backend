# ruff: file-ignore[line-too-long]
"""Candidate Portal assessments (Epic 40 A6): ``/api/v1/portal/assessments``.

``Depends(current_candidate)`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations). Every endpoint is
scoped to the signed-in Candidate's own invitations (another Candidate's invitation -> 404). Responses are the
candidate-facing delivery models (no answer keys, rubrics or item ids). The attempt session id travels in the
``X-Attempt-Session`` header. Each endpoint calls one ``taking`` function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response

from app.core.context import get_context
from app.dependencies.candidate import current_candidate
from app.schemas.assessments_delivery import (
    AttemptView,
    PortalInvitationOut,
    PreStartOut,
    SaveIn,
    SaveOut,
    SignalsAcceptedOut,
    SignalsIn,
    StartIn,
    SubmitOut,
)
from app.services.assessments import taking

router = APIRouter(prefix="/portal/assessments", tags=["portal"])

Candidate = Annotated[Any, Depends(current_candidate)]
Session = Annotated[str | None, Header(alias="X-Attempt-Session", max_length=200)]
NO_STORE = {"Cache-Control": "no-store"}


def _subject(principal: Any, invitation_id: uuid.UUID, response: Response) -> taking.Subject:
    response.headers.update(NO_STORE)
    candidate_id = getattr(principal, "candidate_id", None) or principal.id
    return taking.portal_subject(principal.tenant_id, candidate_id, invitation_id, get_context().ip)


@router.get("", response_model=list[PortalInvitationOut])
async def list_mine(principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    candidate_id = getattr(principal, "candidate_id", None) or principal.id
    return await taking.portal_list(principal.tenant_id, candidate_id)


@router.get("/{invitation_id}", response_model=PreStartOut)
async def prestart(invitation_id: uuid.UUID, principal: Candidate, response: Response) -> Any:
    return await taking.prestart(_subject(principal, invitation_id, response))


@router.post("/{invitation_id}/start", response_model=AttemptView)
async def start(
    invitation_id: uuid.UUID, body: StartIn, principal: Candidate, response: Response, session: Session = None
) -> Any:
    return await taking.start(_subject(principal, invitation_id, response), body, session)


@router.get("/{invitation_id}/attempt", response_model=AttemptView)
async def attempt(
    invitation_id: uuid.UUID, principal: Candidate, response: Response, session: Session = None
) -> Any:
    return await taking.attempt(_subject(principal, invitation_id, response), session)


@router.put("/{invitation_id}/attempt/answers", response_model=SaveOut)
async def save(
    invitation_id: uuid.UUID, body: SaveIn, principal: Candidate, response: Response, session: Session = None
) -> Any:
    return await taking.save(_subject(principal, invitation_id, response), session, body)


@router.post("/{invitation_id}/attempt/sections/{key}/finish", response_model=AttemptView)
async def finish_section(
    invitation_id: uuid.UUID, key: str, principal: Candidate, response: Response, session: Session = None
) -> Any:
    return await taking.finish_section(_subject(principal, invitation_id, response), session, key)


@router.post("/{invitation_id}/attempt/signals", response_model=SignalsAcceptedOut)
async def signals(
    invitation_id: uuid.UUID,
    body: SignalsIn,
    principal: Candidate,
    response: Response,
    session: Session = None,
) -> Any:
    return await taking.signals(_subject(principal, invitation_id, response), session, body)


@router.post("/{invitation_id}/attempt/submit", response_model=SubmitOut)
async def submit(
    invitation_id: uuid.UUID, principal: Candidate, response: Response, session: Session = None
) -> Any:
    return await taking.submit(_subject(principal, invitation_id, response), session)
