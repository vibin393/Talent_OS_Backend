# ruff: noqa: E501
"""Candidate Portal self-scheduling (Epic 59 A6): ``/api/v1/portal/scheduling`` and the reschedule request.

``Depends(current_candidate)`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations). Every endpoint is scoped
to the signed-in Candidate's own offers / interviews (another Candidate's -> 404). Responses are the Candidate-facing
projection only."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from app.core.database import tenant_session
from app.dependencies.candidate import current_candidate
from app.schemas.scheduling_public import (
    CounterIn,
    PickIn,
    PickResultOut,
    PortalRescheduleIn,
    PortalRescheduleOut,
    SlotPickView,
)
from app.services import scheduling_agent_service as svc

router = APIRouter(prefix="/portal", tags=["portal"])
Candidate = Annotated[Any, Depends(current_candidate)]
NO_STORE = {"Cache-Control": "no-store"}


def _candidate_id(principal: Any) -> uuid.UUID:
    return getattr(principal, "candidate_id", None) or principal.id


@router.get("/scheduling", response_model=list[SlotPickView])
async def list_offers(principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return await svc.offers.portal_list(principal.tenant_id, _candidate_id(principal))


@router.post("/scheduling/{offer_id}/pick", response_model=PickResultOut)
async def pick(offer_id: uuid.UUID, body: PickIn, principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    subject = svc.offers.portal_subject(principal.tenant_id, _candidate_id(principal), offer_id)
    out, dedup = await svc.offers.pick(subject, body.index, body.pick_id)
    return PickResultOut(status=out.status, deduplicated=dedup, view=out)


@router.post("/scheduling/{offer_id}/counter", response_model=SlotPickView)
async def counter(offer_id: uuid.UUID, body: CounterIn, principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    subject = svc.offers.portal_subject(principal.tenant_id, _candidate_id(principal), offer_id)
    return await svc.offers.counter(subject, body.windows, body.note)


@router.post("/interviews/{interview_id}/reschedule-request", response_model=PortalRescheduleOut)
async def reschedule_request(
    interview_id: uuid.UUID, body: PortalRescheduleIn, principal: Candidate, response: Response
) -> Any:
    response.headers.update(NO_STORE)
    from app.services.scheduling_agent.common import system_ctx

    async with tenant_session(principal.tenant_id) as db:
        req = await svc.requests.request_reschedule(
            system_ctx(principal.tenant_id), db, interview_id, by="candidate", reason=body.reason,
            candidate_id=_candidate_id(principal),
        )  # fmt: skip
        return PortalRescheduleOut(status=req.status)
