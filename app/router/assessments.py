# ruff: noqa: E501
"""Assessments, internal API (Epic 40, FR-202..FR-208): ``/api/v1/assessment-items``, ``/assessments``,
``/assessment-invitations``, ``/assessment-reviews``, ``/attempt-answers``, ``/attempts``, ``/assessment-settings``,
``/candidates/{id}/assessment-results``, ``/applications/assessment-badges``, ``/submissions/{id}/assessment-shares``.

Permissions: ``assessment:read`` (results, invitations, signals), ``assessment:author`` (items, import, assemble,
publish), ``assessment:invite`` (invite, extend, cancel), ``assessment:review`` (review queue, scores, Pass / Fail
(human only), annotations, Manual results), ``assessment:configure`` (settings, provider catalogue / skill map).
Each endpoint calls one service function. Mounted before the P1 pipeline routers (``/applications/assessment-badges``
precedes ``/applications/{id}``).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.core.context import get_context
from app.core.errors import ConflictError
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.assessments import (
    AiSuggestionOut,
    AnnotationIn,
    AnnotationOut,
    AssessmentCreate,
    AssessmentDetailOut,
    AssessmentInviteIn,
    AssessmentOut,
    AssessmentPatch,
    BadgesOut,
    BulkInviteIn,
    BulkInviteOut,
    CancelIn,
    CatalogueSyncIn,
    DecideIn,
    DraftIn,
    ExtendIn,
    ImportCommitOut,
    ImportPreviewOut,
    InvitationOut,
    InvitationPatch,
    ItemBody,
    ItemCreate,
    ItemOut,
    ItemVersionOut,
    ManualResultIn,
    ProviderTestOut,
    PublishOut,
    ReviewIn,
    ReviewItemOut,
    SettingsIn,
    SettingsOut,
    SharesIn,
    SharesOut,
    SignalsOut,
    SkillMapIn,
    SkillMapOut,
)
from app.services import files_service
from app.services.assessments import (
    assembly,
    invitations,
    item_import,
    items,
    proctoring,
    provider,
    review,
    settings,
    shares,
)

router = APIRouter(tags=["assessments"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("assessment:read"))]
ReadOrAuthor = Annotated[Principal, Depends(require_any("assessment:read", "assessment:author"))]
Author = Annotated[Principal, Depends(require("assessment:author"))]
Invite = Annotated[Principal, Depends(require("assessment:invite"))]
Review = Annotated[Principal, Depends(require("assessment:review"))]
Configure = Annotated[Principal, Depends(require("assessment:configure"))]
MAX_IMPORT_MB = 5


# ================================================================ items
@router.get("/assessment-items", response_model=list[ItemOut])
async def list_items(
    _: ReadOrAuthor, db: DB, q: str | None = None, kind: str | None = None, include_archived: bool = False
) -> Any:
    return await items.list_items(get_context(), db, q=q, kind=kind, include_archived=include_archived)


@router.post("/assessment-items/import", response_model=ImportPreviewOut)
async def import_preview(_: Author, db: DB, file: Annotated[UploadFile, File()]) -> Any:
    data = await files_service.read_upload(file, MAX_IMPORT_MB)
    return await item_import.preview(get_context(), db, data, file.filename)


@router.post("/assessment-items/import/{batch_id}/commit", response_model=ImportCommitOut)
async def import_commit(batch_id: uuid.UUID, _: Author, db: DB) -> Any:
    return await item_import.commit(get_context(), db, batch_id)


@router.post("/assessment-items", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def create_item(body: ItemCreate, _: Author, db: DB) -> Any:
    return await items.create_item(get_context(), db, body)


@router.get("/assessment-items/{item_id}", response_model=ItemOut)
async def get_item(item_id: uuid.UUID, _: ReadOrAuthor, db: DB) -> Any:
    return await items.get_item(get_context(), db, item_id)


@router.put("/assessment-items/{item_id}", response_model=ItemOut)
async def update_item(item_id: uuid.UUID, body: ItemBody, _: Author, db: DB) -> Any:
    return await items.update_item(get_context(), db, item_id, body)


@router.post("/assessment-items/{item_id}/archive", response_model=ItemOut)
async def archive_item(item_id: uuid.UUID, _: Author, db: DB) -> Any:
    return await items.archive_item(get_context(), db, item_id)


@router.get("/assessment-items/{item_id}/versions", response_model=list[ItemVersionOut])
async def item_versions(item_id: uuid.UUID, _: ReadOrAuthor, db: DB) -> Any:
    return await items.list_versions(get_context(), db, item_id)


# ================================================================ settings
@router.get("/assessment-settings", response_model=SettingsOut)
async def get_settings(_: ReadOrAuthor, db: DB) -> Any:
    return await settings.get(get_context(), db)


@router.put("/assessment-settings", response_model=SettingsOut)
async def put_settings(body: SettingsIn, _: Configure, db: DB) -> Any:
    return await settings.update(get_context(), db, body)


# ================================================================ assessments (static paths first)
@router.get("/assessments/provider-tests", response_model=list[ProviderTestOut])
async def provider_tests(_: ReadOrAuthor, db: DB, config_id: uuid.UUID | None = None) -> Any:
    return [
        ProviderTestOut.model_validate(r) for r in await assembly.provider_tests(get_context(), db, config_id)
    ]


@router.post("/assessments/provider-catalogue/sync", response_model=list[ProviderTestOut])
async def sync_catalogue(body: CatalogueSyncIn, _: Configure, db: DB) -> Any:
    return await provider.sync_catalogue(get_context(), db, body.config_id)


@router.get("/assessments/skill-map", response_model=SkillMapOut)
async def get_skill_map(_: Configure, db: DB, config_id: uuid.UUID) -> Any:
    return await provider.get_skill_map(get_context(), db, config_id)


@router.put("/assessments/skill-map", response_model=SkillMapOut)
async def put_skill_map(body: SkillMapIn, _: Configure, db: DB) -> Any:
    return await provider.put_skill_map(get_context(), db, body)


@router.get("/assessments", response_model=list[AssessmentOut])
async def list_assessments(
    _: ReadOrAuthor, db: DB, status_: Annotated[str | None, Query(alias="status")] = None
) -> Any:
    return await assembly.list_assessments(get_context(), db, status_)


@router.post("/assessments", response_model=AssessmentDetailOut, status_code=status.HTTP_201_CREATED)
async def create_assessment(body: AssessmentCreate, _: Author, db: DB) -> Any:
    return await assembly.create(get_context(), db, body)


@router.get("/assessments/{assessment_id}", response_model=AssessmentDetailOut)
async def get_assessment(assessment_id: uuid.UUID, _: ReadOrAuthor, db: DB) -> Any:
    return await assembly.get(get_context(), db, assessment_id)


@router.patch("/assessments/{assessment_id}", response_model=AssessmentDetailOut)
async def patch_assessment(assessment_id: uuid.UUID, body: AssessmentPatch, _: Author, db: DB) -> Any:
    return await assembly.update(get_context(), db, assessment_id, body)


@router.put("/assessments/{assessment_id}/draft", response_model=AssessmentDetailOut)
async def save_draft(assessment_id: uuid.UUID, body: DraftIn, _: Author, db: DB) -> Any:
    return await assembly.save_draft(get_context(), db, assessment_id, body)


@router.post("/assessments/{assessment_id}/publish", response_model=PublishOut)
async def publish(assessment_id: uuid.UUID, _: Author, db: DB) -> Any:
    return await assembly.publish(get_context(), db, assessment_id)


@router.post("/assessments/{assessment_id}/archive", response_model=AssessmentDetailOut)
async def archive(assessment_id: uuid.UUID, _: Author, db: DB) -> Any:
    return await assembly.archive(get_context(), db, assessment_id)


@router.post("/assessments/{assessment_id}/invitations/bulk", response_model=BulkInviteOut)
async def bulk_invite(assessment_id: uuid.UUID, body: BulkInviteIn, _: Invite, db: DB) -> Any:
    members: list[uuid.UUID] = []
    if body.pool_id is not None:
        from app.services import (
            pools_service,  # Epic 38 facade; the assessments domain never imports talent_pools
        )

        try:
            members = await pools_service.member_ids(get_context(), db, body.pool_id, limit=500)
        except NotImplementedError as exc:
            raise ConflictError("Talent pools are not available yet", code="pool_unavailable") from exc
    return await invitations.bulk_invite(get_context(), db, assessment_id, body, members)


# ================================================================ invitations
@router.post("/assessment-invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
async def invite(body: AssessmentInviteIn, _: Invite, db: DB) -> Any:
    out = await invitations.invite(get_context(), db, body)
    return await invitations.detail(get_context(), db, out.id)


@router.get("/assessment-invitations", response_model=list[InvitationOut])
async def list_invitations(
    _: Read,
    db: DB,
    assessment_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> Any:
    return await invitations.list_invitations(
        get_context(),
        db,
        assessment_id=assessment_id,
        candidate_id=candidate_id,
        application_id=application_id,
        status=status_,
    )


@router.get("/assessment-invitations/{invitation_id}", response_model=InvitationOut)
async def get_invitation(invitation_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await invitations.detail(get_context(), db, invitation_id)


@router.patch("/assessment-invitations/{invitation_id}", response_model=InvitationOut)
async def patch_invitation(invitation_id: uuid.UUID, body: InvitationPatch, _: Invite, db: DB) -> Any:
    return await invitations.patch(get_context(), db, invitation_id, body)


@router.post("/assessment-invitations/{invitation_id}/extend", response_model=InvitationOut)
async def extend(invitation_id: uuid.UUID, body: ExtendIn, _: Invite, db: DB) -> Any:
    return await invitations.extend(get_context(), db, invitation_id, body)


@router.post("/assessment-invitations/{invitation_id}/cancel", response_model=InvitationOut)
async def cancel(invitation_id: uuid.UUID, body: CancelIn, _: Invite, db: DB) -> Any:
    return await invitations.cancel(get_context(), db, invitation_id, body)


@router.post("/assessment-invitations/{invitation_id}/decide", response_model=InvitationOut)
async def decide(invitation_id: uuid.UUID, body: DecideIn, _: Review, db: DB) -> Any:
    return await invitations.decide(get_context(), db, invitation_id, body)


@router.post("/assessment-invitations/{invitation_id}/manual-result", response_model=InvitationOut)
async def manual_result(invitation_id: uuid.UUID, body: ManualResultIn, _: Review, db: DB) -> Any:
    return await provider.manual_result(get_context(), db, invitation_id, body)


@router.get("/candidates/{candidate_id}/assessment-results", response_model=list[InvitationOut])
async def candidate_results(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await invitations.candidate_results(get_context(), db, candidate_id)


@router.get("/applications/assessment-badges", response_model=BadgesOut)
async def badges(_: Read, db: DB, ids: Annotated[str, Query(max_length=8000)] = "") -> Any:
    parsed: list[uuid.UUID] = []
    for raw in ids.split(","):
        try:
            parsed.append(uuid.UUID(raw.strip()))
        except ValueError:
            continue
    return await invitations.badges(get_context(), db, parsed[:200])


# ================================================================ reviews / proctoring
@router.get("/assessment-reviews", response_model=list[ReviewItemOut])
async def review_queue(_: Review, db: DB, status_: Annotated[str, Query(alias="status")] = "pending") -> Any:
    return await review.queue(get_context(), db, status_)


@router.post("/attempt-answers/{answer_id}/review", response_model=ReviewItemOut)
async def review_answer(answer_id: uuid.UUID, body: ReviewIn, _: Review, db: DB) -> Any:
    return await review.review(get_context(), db, answer_id, body)


@router.post("/attempt-answers/{answer_id}/ai-suggestion", response_model=AiSuggestionOut)
async def ai_suggestion(answer_id: uuid.UUID, _: Review, db: DB) -> Any:
    return await review.ai_suggestion(get_context(), db, answer_id)


@router.get("/attempts/{attempt_id}/answers", response_model=list[ReviewItemOut])
async def attempt_answers(attempt_id: uuid.UUID, _: Review, db: DB) -> Any:
    return await review.attempt_answers(get_context(), db, attempt_id)


@router.get("/attempts/{attempt_id}/signals", response_model=SignalsOut)
async def signals(attempt_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await proctoring.list_signals(get_context(), db, attempt_id)


@router.post(
    "/attempts/{attempt_id}/signal-annotations",
    response_model=AnnotationOut,
    status_code=status.HTTP_201_CREATED,
)
async def annotate(attempt_id: uuid.UUID, body: AnnotationIn, _: Review, db: DB) -> Any:
    return await proctoring.annotate(get_context(), db, attempt_id, body)


# ================================================================ submission shares (FR-208)
@router.get("/submissions/{submission_id}/assessment-shares", response_model=SharesOut)
async def get_shares(submission_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await shares.get_shares(get_context(), db, submission_id)


@router.put("/submissions/{submission_id}/assessment-shares", response_model=SharesOut)
async def put_shares(submission_id: uuid.UUID, body: SharesIn, _: Read, db: DB) -> Any:
    return await shares.put_shares(get_context(), db, submission_id, body)
