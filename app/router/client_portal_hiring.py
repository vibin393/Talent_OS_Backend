"""Client Portal hiring (Epic 18, FR-100/FR-101): ``/api/v1/client-portal`` demands, demand requests, sent
submissions, comparison, feedback, resume, interview slots and the Client User's scorecards. Client session
(org RLS ``client``) + portal permissions; every response is FR-80 guarded."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import ValidationError

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, ClientDep, require_portal_permission
from app.schemas.demands_portal import ClientDemandRequestIn, ClientDemandRequestOut, ClientDemandSummary
from app.schemas.interviews import PublicScorecardIn, PublicScorecardView
from app.schemas.interviews_portal import ClientScorecardItem, ClientUpcomingInterview
from app.schemas.pipeline import PublicActionOut
from app.schemas.pipeline_portal import (
    ClientChooseSlotIn,
    ClientCompareIn,
    ClientCompareOut,
    ClientFeedbackIn,
    ClientSlotProposalIn,
    ClientSlotProposalOut,
    ClientSubmissionOut,
)
from app.services.demands import portal as demands_portal
from app.services.interviews import portal as interviews_portal
from app.services.pipeline import portal as pipeline_portal

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
Reviewer = Annotated[Principal, Depends(require_portal_permission("submissions:review"))]


# ---------------------------------------------------------------- demands (any active Client User)
@router.get("/demands", response_model=list[ClientDemandSummary])
async def list_demands(
    _: ClientDep, db: ClientDBSessionDep, status_: Annotated[str | None, Query(alias="status")] = None
) -> Any:
    return await demands_portal.client_list_demands(get_context(), db, status_)


@router.get("/demands/{demand_id}", response_model=ClientDemandSummary)
async def get_demand(demand_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Any:
    return await demands_portal.client_get_demand(get_context(), db, demand_id)


@router.post("/demand-requests", response_model=ClientDemandRequestOut, status_code=status.HTTP_201_CREATED)
async def request_demand(
    _: ClientDep,
    payload: Annotated[str, Form(description="JSON body of ClientDemandRequestIn")],
    jd: Annotated[UploadFile | None, File()] = None,
) -> Any:
    from app.services import files_service

    try:
        data = ClientDemandRequestIn.model_validate_json(payload)
    except ValidationError as exc:
        raise ValidationFailed(
            "Invalid demand request",
            code="invalid_request",
            details={"errors": exc.errors(include_url=False, include_context=False)},
        ) from exc
    upload = (await files_service.read_upload(jd), jd.filename) if jd is not None else None
    return await demands_portal.client_create_request(get_context(), data, upload)


# ---------------------------------------------------------------- submissions
@router.get("/submissions", response_model=list[ClientSubmissionOut])
async def list_submissions(_: Reviewer, db: ClientDBSessionDep, demand_id: uuid.UUID | None = None) -> Any:
    return await pipeline_portal.client_list_submissions(get_context(), db, demand_id)


@router.post("/submissions/compare", response_model=ClientCompareOut)
async def compare(data: ClientCompareIn, _: Reviewer, db: ClientDBSessionDep) -> Any:
    return await pipeline_portal.client_compare(get_context(), db, data.submission_ids)


@router.get("/submissions/{submission_id}", response_model=ClientSubmissionOut)
async def get_submission(submission_id: uuid.UUID, _: Reviewer, db: ClientDBSessionDep) -> Any:
    return await pipeline_portal.client_get_submission(get_context(), db, submission_id)


@router.get("/submissions/{submission_id}/resume")
async def resume(submission_id: uuid.UUID, _: Reviewer, db: ClientDBSessionDep) -> Response:
    data, filename, ctype = await pipeline_portal.client_resume(get_context(), db, submission_id)
    return Response(
        content=data, media_type=ctype, headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


@router.post("/submissions/{submission_id}/feedback", response_model=ClientSubmissionOut)
async def feedback(
    submission_id: uuid.UUID, data: ClientFeedbackIn, _: Reviewer, db: ClientDBSessionDep
) -> Any:
    return await pipeline_portal.client_feedback(
        get_context(), db, submission_id, data.decision, data.comment
    )


@router.post(
    "/submissions/{submission_id}/slot-proposals",
    response_model=ClientSlotProposalOut,
    status_code=status.HTTP_201_CREATED,
)
async def propose_slots(
    submission_id: uuid.UUID, data: ClientSlotProposalIn, _: Reviewer, db: ClientDBSessionDep
) -> Any:
    return await pipeline_portal.client_propose_slots(get_context(), db, submission_id, data.slots)


@router.post("/slot-offers/{proposal_id}/choose", response_model=ClientSlotProposalOut)
async def choose_slot(
    proposal_id: uuid.UUID, data: ClientChooseSlotIn, _: Reviewer, db: ClientDBSessionDep
) -> Any:
    return await pipeline_portal.client_choose_slot(get_context(), db, proposal_id, data.index)


# ---------------------------------------------------------------- interviews / scorecards
@router.get("/interviews/upcoming", response_model=list[ClientUpcomingInterview])
async def upcoming(_: Reviewer, db: ClientDBSessionDep) -> Any:
    return await interviews_portal.client_upcoming_interviews(get_context(), db)


@router.get("/scorecards", response_model=list[ClientScorecardItem])
async def my_scorecards(_: ClientDep, db: ClientDBSessionDep) -> Any:
    return await interviews_portal.client_my_scorecards(get_context(), db)


@router.get("/scorecards/{participant_id}", response_model=PublicScorecardView)
async def scorecard(participant_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Any:
    return await interviews_portal.client_scorecard_view(get_context(), db, participant_id)


@router.post("/scorecards/{participant_id}", response_model=PublicActionOut)
async def submit_scorecard(
    participant_id: uuid.UUID, data: PublicScorecardIn, _: ClientDep, db: ClientDBSessionDep
) -> Any:
    return PublicActionOut(
        status=await interviews_portal.client_submit_scorecard(get_context(), db, participant_id, data)
    )
