# ruff: noqa: E501
"""Candidate Portal self-identification and alternative-process requests (Epic 49 A3/A8):
``/api/v1/portal/self-id`` and ``/api/v1/portal/alternative-requests``.

``CandidateDep`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations); ``CandidateDBDep`` is the worker-
scoped session, so RLS returns the Candidate's own self-ID row only. ``GET /portal/self-id`` is the only individual
read of self-ID answers in the product.
"""

from typing import Any

from fastapi import APIRouter, Response, status

import app.models.fairness  # noqa: F401  - fairness tables on Base.metadata at app start (AD-47)
from app.core.context import get_context
from app.dependencies.candidate import CandidateDBDep, CandidateDep
from app.schemas.fairness import AlternativeRequestIn, AlternativeRequestOut, SelfIdIn, SelfIdOut
from app.services.fairness import alternatives, self_id

router = APIRouter(prefix="/portal", tags=["portal"])
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/self-id", response_model=SelfIdOut)
async def get_self_id(principal: CandidateDep, db: CandidateDBDep, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return await self_id.portal_get(get_context(), db, principal.candidate_id)


@router.put("/self-id", response_model=SelfIdOut)
async def put_self_id(principal: CandidateDep, db: CandidateDBDep, body: SelfIdIn, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return await self_id.upsert_from_portal(get_context(), db, principal.candidate_id, body)


@router.delete("/self-id", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_self_id(principal: CandidateDep, db: CandidateDBDep) -> Response:
    await self_id.delete_from_portal(get_context(), db, principal.candidate_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/alternative-requests", response_model=list[AlternativeRequestOut])
async def list_alternative_requests(principal: CandidateDep, db: CandidateDBDep) -> Any:
    return await alternatives.list_for_candidate(db, principal.candidate_id)


@router.post(
    "/alternative-requests", response_model=AlternativeRequestOut, status_code=status.HTTP_201_CREATED
)
async def request_alternative(principal: CandidateDep, body: AlternativeRequestIn) -> Any:
    return await alternatives.from_portal(principal.tenant_id, principal.candidate_id, body)
