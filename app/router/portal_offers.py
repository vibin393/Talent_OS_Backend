"""Candidate Portal offer API (Epic 8 A6; Epic 9 provides the candidate principal and the portal UI).

``Depends(current_candidate)`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations). Every endpoint
is scoped to the signed-in candidate's own offers (another candidate's offer -> 404) and only offers that were
released are visible. Views never contain bill rate, burden or margin. ``GET`` endpoints record nothing: the
page POSTs ``viewed`` once from JavaScript.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.core.context import get_context
from app.dependencies.candidate import current_candidate
from app.schemas.offers import AcceptIn, CandidateOfferList, CandidateOfferView, DeclineIn, OfferActionOut
from app.services import offers_service as svc

router = APIRouter(prefix="/portal/offers", tags=["portal"])

Candidate = Annotated[Any, Depends(current_candidate)]
NO_STORE = {"Cache-Control": "no-store"}


def _ids(principal: Any) -> tuple[uuid.UUID, uuid.UUID]:
    candidate_id = getattr(principal, "candidate_id", None) or principal.id
    return principal.tenant_id, candidate_id


@router.get("", response_model=CandidateOfferList)
async def list_offers(principal: Candidate) -> Any:
    tenant_id, candidate_id = _ids(principal)
    return CandidateOfferList(items=await svc.response.portal_list(tenant_id, candidate_id))


@router.get("/{offer_id}", response_model=CandidateOfferView)
async def get_offer(offer_id: uuid.UUID, principal: Candidate) -> Any:
    tenant_id, candidate_id = _ids(principal)
    return await svc.response.portal_get(tenant_id, candidate_id, offer_id)


@router.post("/{offer_id}/viewed", response_model=CandidateOfferView)
async def viewed(offer_id: uuid.UUID, principal: Candidate, request: Request) -> Any:
    tenant_id, candidate_id = _ids(principal)
    return await svc.response.portal_viewed(
        tenant_id, candidate_id, offer_id, get_context().ip, request.headers.get("user-agent")
    )


@router.get("/{offer_id}/letter", response_class=Response)
async def letter(offer_id: uuid.UUID, principal: Candidate) -> Response:
    tenant_id, candidate_id = _ids(principal)
    data, filename = await svc.response.portal_letter(tenant_id, candidate_id, offer_id)
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "offer.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={**NO_STORE, "Content-Disposition": f'inline; filename="{safe}"'},
    )


@router.post("/{offer_id}/accept", response_model=OfferActionOut)
async def accept(offer_id: uuid.UUID, body: AcceptIn, principal: Candidate, request: Request) -> Any:
    tenant_id, candidate_id = _ids(principal)
    return await svc.response.portal_accept(
        tenant_id,
        candidate_id,
        offer_id,
        typed_name=body.typed_name,
        version_no=body.version_no,
        agreed=body.agreed,
        ip=get_context().ip,
        user_agent=request.headers.get("user-agent"),
        otp=body.otp,
    )


@router.post("/{offer_id}/decline", response_model=OfferActionOut)
async def decline(offer_id: uuid.UUID, body: DeclineIn, principal: Candidate, request: Request) -> Any:
    tenant_id, candidate_id = _ids(principal)
    return await svc.response.portal_decline(
        tenant_id,
        candidate_id,
        offer_id,
        reason=body.reason,
        version_no=body.version_no,
        ip=get_context().ip,
        user_agent=request.headers.get("user-agent"),
    )
