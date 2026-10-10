"""Pipeline endpoints (Epic 6): settings / stages / reason codes / questionnaires under ``/pipeline``, the
demand-scoped pipeline, applications, screenings, submissions, client feedback, candidate applications and
consent requests, and secure-link revocation. Each endpoint calls one service function."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.pipeline import (
    AIScreeningCreate,
    ApplicationDetailOut,
    ApplicationOut,
    ApplicationsCreate,
    ApplicationsCreateOut,
    ApplyUpdatesIn,
    ApplyUpdatesOut,
    BoardOut,
    BulkTransitionIn,
    BulkTransitionOut,
    CandidateApplicationOut,
    ConsentRequestIn,
    FeedbackIn,
    FeedbackOut,
    MarginOut,
    MarginPreviewIn,
    OutcomeIn,
    PipelineSettingsOut,
    PipelineSettingsUpdate,
    QuestionnaireCreate,
    QuestionnaireOut,
    QuestionnaireUpdate,
    ReasonCodeCreate,
    ReasonCodeOut,
    ReasonCodeUpdate,
    ScreeningCreate,
    ScreeningFormOut,
    ScreeningOut,
    ScreeningUpdate,
    SecureLinkOut,
    StageArchive,
    StageCreate,
    StageOut,
    StageReorder,
    StagesOut,
    StageUpdate,
    SubmissionOut,
    SubmissionPreviewIn,
    SubmissionPreviewOut,
    SubmissionsCreate,
    SubmissionsCreateOut,
    TransitionIn,
)
from app.schemas.privacy import ConsentRequestResult
from app.services import pipeline as svc

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
demand_router = APIRouter(prefix="/demands", tags=["pipeline"])
applications_router = APIRouter(prefix="/applications", tags=["pipeline"])
screenings_router = APIRouter(prefix="/screenings", tags=["pipeline"])
submissions_router = APIRouter(prefix="/submissions", tags=["submissions"])
feedback_router = APIRouter(prefix="/client-feedback", tags=["submissions"])
misc_router = APIRouter(tags=["pipeline"])

Configure = Annotated[Principal, Depends(require("pipeline:configure"))]
AppRead = Annotated[Principal, Depends(require("application:read"))]
AppCreate = Annotated[Principal, Depends(require("application:create"))]
Screen = Annotated[Principal, Depends(require("screening:write"))]
SubRead = Annotated[Principal, Depends(require("submission:read"))]
SubCreate = Annotated[Principal, Depends(require("submission:create"))]
FeedbackWrite = Annotated[Principal, Depends(require("submission:update"))]
ConsentWrite = Annotated[Principal, Depends(require("consent:write"))]
CompView = Annotated[Principal, Depends(require("compensation:view"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


# ================================================================ settings, stages, reasons, questionnaires
@router.get("/settings", response_model=PipelineSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.settings.get_settings(get_context(), db)


@router.patch("/settings", response_model=PipelineSettingsOut)
async def update_settings(body: PipelineSettingsUpdate, _: Configure, db: DB) -> Any:
    return await svc.settings.update_settings(get_context(), db, body)


@router.get("/stages", response_model=StagesOut)
async def list_stages(_: CurrentUserDep, db: DB, include_transitions: bool = False) -> Any:
    return await svc.stages.list_stages(get_context(), db, include_transitions)


@router.post("/stages", response_model=StageOut, status_code=status.HTTP_201_CREATED)
async def create_stage(body: StageCreate, _: Configure, db: DB) -> Any:
    return await svc.stages.create_stage(get_context(), db, body)


@router.post("/stages/reorder", response_model=StagesOut)
async def reorder_stages(body: StageReorder, _: Configure, db: DB) -> Any:
    return await svc.stages.reorder_stages(get_context(), db, body.stage_ids)


@router.patch("/stages/{stage_id}", response_model=StageOut)
async def update_stage(stage_id: uuid.UUID, body: StageUpdate, _: Configure, db: DB) -> Any:
    return await svc.stages.update_stage(get_context(), db, stage_id, body)


@router.post("/stages/{stage_id}/archive", response_model=StageOut)
async def archive_stage(stage_id: uuid.UUID, body: StageArchive, _: Configure, db: DB) -> Any:
    return await svc.stages.archive_stage(get_context(), db, stage_id, body)


@router.delete("/stages/{stage_id}", response_model=StageOut)
async def delete_stage(stage_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await svc.stages.delete_stage(get_context(), db, stage_id)


@router.get("/reason-codes", response_model=list[ReasonCodeOut])
async def list_reason_codes(
    _: CurrentUserDep, db: DB, kind: str | None = None, include_archived: bool = False
) -> Any:
    return await svc.stages.list_reason_codes(get_context(), db, kind, include_archived)


@router.post("/reason-codes", response_model=ReasonCodeOut, status_code=status.HTTP_201_CREATED)
async def create_reason_code(body: ReasonCodeCreate, _: Configure, db: DB) -> Any:
    return await svc.stages.create_reason_code(get_context(), db, body)


@router.patch("/reason-codes/{reason_id}", response_model=ReasonCodeOut)
async def update_reason_code(reason_id: uuid.UUID, body: ReasonCodeUpdate, _: Configure, db: DB) -> Any:
    return await svc.stages.update_reason_code(get_context(), db, reason_id, body)


@router.post("/reason-codes/{reason_id}/archive", response_model=ReasonCodeOut)
async def archive_reason_code(reason_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await svc.stages.archive_reason_code(get_context(), db, reason_id)


@router.get("/questionnaires", response_model=list[QuestionnaireOut])
async def list_questionnaires(_: CurrentUserDep, db: DB, include_archived: bool = False) -> Any:
    return await svc.screening.list_questionnaires(get_context(), db, include_archived)


@router.post("/questionnaires", response_model=QuestionnaireOut, status_code=status.HTTP_201_CREATED)
async def create_questionnaire(body: QuestionnaireCreate, _: Configure, db: DB) -> Any:
    return await svc.screening.create_questionnaire(get_context(), db, body)


@router.get("/questionnaires/{questionnaire_id}", response_model=QuestionnaireOut)
async def get_questionnaire(questionnaire_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.screening.get_questionnaire(get_context(), db, questionnaire_id)


@router.patch("/questionnaires/{questionnaire_id}", response_model=QuestionnaireOut)
async def update_questionnaire(
    questionnaire_id: uuid.UUID, body: QuestionnaireUpdate, _: Configure, db: DB
) -> Any:
    return await svc.screening.update_questionnaire(get_context(), db, questionnaire_id, body)


@router.post("/questionnaires/{questionnaire_id}/archive", response_model=QuestionnaireOut)
async def archive_questionnaire(questionnaire_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await svc.screening.archive_questionnaire(get_context(), db, questionnaire_id)


# ================================================================ demand-scoped
@demand_router.post("/{demand_id}/applications", response_model=ApplicationsCreateOut)
async def add_candidates(demand_id: uuid.UUID, body: ApplicationsCreate, _: AppCreate, db: DB) -> Any:
    return await svc.applications.add_candidates(get_context(), db, demand_id, body)


@demand_router.get("/{demand_id}/applications", response_model=Page[ApplicationOut])
async def list_applications(
    demand_id: uuid.UUID,
    _: AppRead,
    db: DB,
    params: Paging,
    stage_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> Any:
    return await svc.applications.list_for_demand(get_context(), db, demand_id, params, stage_id, q)


@demand_router.get("/{demand_id}/pipeline/board", response_model=BoardOut)
async def board(
    demand_id: uuid.UUID, _: AppRead, db: DB, q: Annotated[str | None, Query(max_length=200)] = None
) -> Any:
    return await svc.applications.board(get_context(), db, demand_id, q)


@demand_router.post("/{demand_id}/applications/bulk-transition", response_model=BulkTransitionOut)
async def bulk_transition(demand_id: uuid.UUID, body: BulkTransitionIn, _: AppRead, db: DB) -> Any:
    return await svc.applications.bulk_transition(get_context(), db, demand_id, body)


@demand_router.post("/{demand_id}/submissions", response_model=SubmissionsCreateOut)
async def create_submissions(demand_id: uuid.UUID, body: SubmissionsCreate, _: SubCreate, db: DB) -> Any:
    return await svc.submissions.create_submissions(get_context(), db, demand_id, body)


@demand_router.get("/{demand_id}/submissions", response_model=list[SubmissionOut])
async def list_submissions(demand_id: uuid.UUID, _: SubRead, db: DB) -> Any:
    return await svc.submissions.list_for_demand(get_context(), db, demand_id)


# ================================================================ applications
@applications_router.get("/{application_id}", response_model=ApplicationDetailOut)
async def get_application(application_id: uuid.UUID, _: AppRead, db: DB) -> Any:
    return await svc.applications.get_application(get_context(), db, application_id)


@applications_router.post("/{application_id}/transition", response_model=ApplicationDetailOut)
async def transition(application_id: uuid.UUID, body: TransitionIn, _: AppRead, db: DB) -> Any:
    return await svc.applications.transition(get_context(), db, application_id, body)


@applications_router.get("/{application_id}/screening-form", response_model=ScreeningFormOut)
async def screening_form(
    application_id: uuid.UUID, _: AppRead, db: DB, questionnaire_id: uuid.UUID | None = None
) -> Any:
    return await svc.screening.screening_form(get_context(), db, application_id, questionnaire_id)


@applications_router.get("/{application_id}/screenings", response_model=list[ScreeningOut])
async def list_screenings(application_id: uuid.UUID, _: AppRead, db: DB) -> Any:
    return await svc.screening.list_screenings(get_context(), db, application_id)


@applications_router.post(
    "/{application_id}/screenings", response_model=ScreeningOut, status_code=status.HTTP_201_CREATED
)
async def create_screening(application_id: uuid.UUID, body: ScreeningCreate, _: Screen, db: DB) -> Any:
    return await svc.screening.create_screening(get_context(), db, application_id, body)


@applications_router.post(
    "/{application_id}/ai-screenings", response_model=ScreeningOut, status_code=status.HTTP_201_CREATED
)
async def generate_ai_screening(application_id: uuid.UUID, body: AIScreeningCreate, _: Screen, db: DB) -> Any:
    del body  # only action=generate exists
    return await svc.screening.generate_ai_screening(get_context(), db, application_id)


# ================================================================ screenings
@screenings_router.get("/{screening_id}", response_model=ScreeningOut)
async def get_screening(screening_id: uuid.UUID, _: AppRead, db: DB) -> Any:
    return await svc.screening.get_screening(get_context(), db, screening_id)


@screenings_router.patch("/{screening_id}", response_model=ScreeningOut)
async def update_screening(screening_id: uuid.UUID, body: ScreeningUpdate, _: Screen, db: DB) -> Any:
    return await svc.screening.update_screening(get_context(), db, screening_id, body)


@screenings_router.post("/{screening_id}/outcome", response_model=ScreeningOut)
async def set_outcome(screening_id: uuid.UUID, body: OutcomeIn, _: Screen, db: DB) -> Any:
    return await svc.screening.set_outcome(get_context(), db, screening_id, body)


@screenings_router.post("/{screening_id}/apply-candidate-updates", response_model=ApplyUpdatesOut)
async def apply_candidate_updates(screening_id: uuid.UUID, body: ApplyUpdatesIn, _: Screen, db: DB) -> Any:
    return await svc.screening.apply_candidate_updates(get_context(), db, screening_id, list(body.fields))


@screenings_router.post("/{screening_id}/send", response_model=ScreeningOut)
async def send_ai_screening(screening_id: uuid.UUID, _: Screen, db: DB) -> Any:
    return await svc.screening.send_ai_screening(get_context(), db, screening_id)


@screenings_router.post("/{screening_id}/review", response_model=ScreeningOut)
async def review(screening_id: uuid.UUID, _: Screen, db: DB) -> Any:
    return await svc.screening.review(get_context(), db, screening_id)


@screenings_router.post("/{screening_id}/summarize", response_model=ScreeningOut)
async def summarize(screening_id: uuid.UUID, _: Screen, db: DB) -> Any:
    return await svc.screening.request_summary(get_context(), db, screening_id)


# ================================================================ submissions
@submissions_router.post("/margin-preview", response_model=MarginOut)
async def margin_preview(body: MarginPreviewIn, _: CompView, db: DB) -> Any:
    return await svc.submissions.margin_preview(get_context(), db, body)


@submissions_router.post("/preview", response_model=SubmissionPreviewOut)
async def preview(body: SubmissionPreviewIn, _: SubCreate, db: DB) -> Any:
    return await svc.submissions.preview(get_context(), db, body)


@submissions_router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: uuid.UUID, _: SubRead, db: DB) -> Any:
    return await svc.submissions.get_submission(get_context(), db, submission_id)


@submissions_router.post("/{submission_id}/send", response_model=SubmissionOut)
async def send_submission(submission_id: uuid.UUID, _: SubCreate, db: DB) -> Any:
    return await svc.submissions.send(get_context(), db, submission_id)


@submissions_router.post("/{submission_id}/cancel", response_model=SubmissionOut)
async def cancel_submission(submission_id: uuid.UUID, _: SubCreate, db: DB) -> Any:
    return await svc.submissions.cancel(get_context(), db, submission_id)


@submissions_router.get("/{submission_id}/resume", response_class=RedirectResponse, status_code=307)
async def submission_resume(submission_id: uuid.UUID, _: SubRead, db: DB) -> RedirectResponse:
    file_id = await svc.submissions.resume_file(get_context(), db, submission_id)
    return RedirectResponse(f"/api/v1/files/{file_id}/download", status_code=307)


@submissions_router.get("/{submission_id}/feedback", response_model=list[FeedbackOut])
async def list_feedback(submission_id: uuid.UUID, _: SubRead, db: DB) -> Any:
    return await svc.feedback.list_feedback(get_context(), db, submission_id)


@submissions_router.post(
    "/{submission_id}/feedback", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED
)
async def record_feedback(submission_id: uuid.UUID, body: FeedbackIn, _: FeedbackWrite, db: DB) -> Any:
    return await svc.feedback.record(get_context(), db, submission_id, body)


@feedback_router.post("/{feedback_id}/confirm-rejection", response_model=FeedbackOut)
async def confirm_rejection(feedback_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.feedback.confirm_rejection(get_context(), db, feedback_id)


@feedback_router.post("/{feedback_id}/dismiss", response_model=FeedbackOut)
async def dismiss_rejection(feedback_id: uuid.UUID, _: FeedbackWrite, db: DB) -> Any:
    return await svc.feedback.dismiss_rejection(get_context(), db, feedback_id)


# ================================================================ candidates / consents / links
@misc_router.get("/candidates/{candidate_id}/applications", response_model=list[CandidateApplicationOut])
async def candidate_applications(candidate_id: uuid.UUID, _: AppRead, db: DB) -> Any:
    return await svc.applications.candidate_applications(get_context(), db, candidate_id)


@misc_router.post("/candidates/{candidate_id}/consent-requests", response_model=ConsentRequestResult)
async def request_consent(candidate_id: uuid.UUID, body: ConsentRequestIn, _: ConsentWrite, db: DB) -> Any:
    return await svc.links.request_consent(get_context(), db, candidate_id, body)


@misc_router.post("/secure-links/{link_id}/revoke", response_model=SecureLinkOut)
async def revoke_link(link_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.links.revoke_link(get_context(), db, link_id)
