# ruff: noqa: E501
"""Background verification and reference checks -- internal API (Epic 20, FR-110..FR-114).

Permissions: ``bgv:read`` (cases, checks, discrepancies, reasons), ``bgv:manage`` (start / cancel cases, assign /
start / record checks, raise / resolve discrepancies, review documents, ad-hoc cases), ``bgv:configure``
(packages, providers, questionnaires, settings), ★ ``bgv:adjudicate`` (adjudicate, waive; human-only),
``reference_check:request`` (request / review reference checks), ``scorecard:read`` (reference answers).
External principals never reach these routes. Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.bgv import (
    AdjudicateIn,
    AssignIn,
    BgvSettingsOut,
    BgvSettingsUpdate,
    CancelIn,
    CaseCreateIn,
    CaseEventOut,
    CaseListItem,
    CaseOut,
    CaseSummaryOut,
    CheckOut,
    DiscrepancyIn,
    DiscrepancyOut,
    DocumentRequestOut,
    DocumentReviewIn,
    PackageIn,
    PackageOut,
    PackageUpdate,
    ProviderIn,
    ProviderOut,
    ProviderUpdate,
    QuestionnaireIn,
    QuestionnaireOut,
    QuestionnaireUpdate,
    ReferenceCheckOut,
    ReferenceRequestIn,
    ReferenceResponsesOut,
    ResolveIn,
    ResultIn,
    ReviewIn,
    WaiveIn,
)
from app.services.bgv import adjudication, cases, checks, discrepancies, packages, references
from app.services.bgv import status as bgv_status
from app.services.files_service import read_upload

router = APIRouter(tags=["bgv"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("bgv:read"))]
Manage = Annotated[Principal, Depends(require("bgv:manage"))]
Configure = Annotated[Principal, Depends(require("bgv:configure"))]
Adjudicate = Annotated[Principal, Depends(require("bgv:adjudicate"))]
RefRequest = Annotated[Principal, Depends(require("reference_check:request"))]
RefRead = Annotated[Principal, Depends(require_any("bgv:read", "reference_check:request"))]
Answers = Annotated[Principal, Depends(require("scorecard:read"))]
ConfigRead = Annotated[Principal, Depends(require_any("bgv:read", "bgv:configure"))]


# ---------------------------------------------------------------- configuration
@router.get("/bgv/settings", response_model=BgvSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await packages.get_settings(get_context(), db)


@router.patch("/bgv/settings", response_model=BgvSettingsOut)
async def update_settings(data: BgvSettingsUpdate, _: Configure, db: DB) -> Any:
    return await packages.update_settings(get_context(), db, data)


@router.get("/bgv/packages", response_model=list[PackageOut])
async def list_packages(_: ConfigRead, db: DB, include_archived: bool = False) -> Any:
    return await packages.list_packages(get_context(), db, include_archived)


@router.post("/bgv/packages", response_model=PackageOut, status_code=status.HTTP_201_CREATED)
async def create_package(data: PackageIn, _: Configure, db: DB) -> Any:
    return await packages.create_package(get_context(), db, data)


@router.patch("/bgv/packages/{package_id}", response_model=PackageOut)
async def update_package(package_id: uuid.UUID, data: PackageUpdate, _: Configure, db: DB) -> Any:
    return await packages.update_package(get_context(), db, package_id, data)


@router.post("/bgv/packages/{package_id}/archive", response_model=PackageOut)
async def archive_package(package_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await packages.archive_package(get_context(), db, package_id)


@router.get("/bgv/providers", response_model=list[ProviderOut])
async def list_providers(_: ConfigRead, db: DB) -> Any:
    return await packages.list_providers(get_context(), db)


@router.post("/bgv/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(data: ProviderIn, _: Configure, db: DB) -> Any:
    return await packages.create_provider(get_context(), db, data)


@router.patch("/bgv/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: uuid.UUID, data: ProviderUpdate, _: Configure, db: DB) -> Any:
    return await packages.update_provider(get_context(), db, provider_id, data)


@router.get("/bgv/reference-questionnaires", response_model=list[QuestionnaireOut])
async def list_questionnaires(_: RefRead, db: DB) -> Any:
    return await packages.list_questionnaires(get_context(), db)


@router.post(
    "/bgv/reference-questionnaires", response_model=QuestionnaireOut, status_code=status.HTTP_201_CREATED
)
async def create_questionnaire(data: QuestionnaireIn, _: Configure, db: DB) -> Any:
    return await packages.create_questionnaire(get_context(), db, data)


@router.patch("/bgv/reference-questionnaires/{questionnaire_id}", response_model=QuestionnaireOut)
async def update_questionnaire(
    questionnaire_id: uuid.UUID, data: QuestionnaireUpdate, _: Configure, db: DB
) -> Any:
    return await packages.update_questionnaire(get_context(), db, questionnaire_id, data)


@router.get("/bgv/status-machine")
async def status_machine(_: Read) -> Any:
    return bgv_status.describe()


# ---------------------------------------------------------------- cases
@router.get("/bgv-cases", response_model=list[CaseListItem])
async def list_cases(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    client_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
    overdue: bool | None = None,
) -> Any:
    return await cases.list_cases(
        get_context(),
        db,
        status=status_,
        client_id=client_id,
        assignee_user_id=assignee_user_id,
        candidate_id=candidate_id,
        placement_id=placement_id,
        overdue=overdue,
    )


@router.get("/bgv-cases/{case_id}", response_model=CaseOut)
async def get_case(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.detail(get_context(), db, case_id)


@router.get("/bgv-cases/{case_id}/timeline", response_model=list[CaseEventOut])
async def case_timeline(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.timeline(get_context(), db, case_id)


@router.post("/bgv-cases/{case_id}/start", response_model=CaseOut)
async def start_case(case_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await cases.start_case(get_context(), db, case_id)


@router.post("/bgv-cases/{case_id}/cancel", response_model=CaseOut)
async def cancel_case(case_id: uuid.UUID, data: CancelIn, _: Manage, db: DB) -> Any:
    return await cases.cancel_case(get_context(), db, case_id, data.reason)


@router.post("/bgv-cases/{case_id}/adjudicate", response_model=CaseOut)
async def adjudicate(case_id: uuid.UUID, data: AdjudicateIn, _: Adjudicate, db: DB) -> Any:
    return await adjudication.adjudicate(get_context(), db, case_id, data)


@router.post("/bgv-cases/{case_id}/documents/{key}/review", response_model=DocumentRequestOut)
async def review_document(case_id: uuid.UUID, key: str, data: DocumentReviewIn, _: Manage, db: DB) -> Any:
    return await cases.review_document(get_context(), db, case_id, key, data)


@router.post(
    "/candidates/{candidate_id}/bgv-cases", response_model=CaseOut, status_code=status.HTTP_201_CREATED
)
async def create_adhoc_case(candidate_id: uuid.UUID, data: CaseCreateIn, _: Manage, db: DB) -> Any:
    return await cases.create_adhoc(get_context(), db, candidate_id, data.package_id, data.placement_id)


@router.get("/candidates/{candidate_id}/bgv-cases", response_model=list[CaseSummaryOut])
async def candidate_cases(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.summaries(get_context(), db, candidate_id=candidate_id)


@router.get("/placements/{placement_id}/bgv", response_model=list[CaseSummaryOut])
async def placement_cases(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.summaries(get_context(), db, placement_id=placement_id)


# ---------------------------------------------------------------- checks
@router.patch("/bgv-checks/{check_id}", response_model=CheckOut)
async def assign_check(check_id: uuid.UUID, data: AssignIn, _: Manage, db: DB) -> Any:
    return await checks.assign(get_context(), db, check_id, data)


@router.post("/bgv-checks/{check_id}/start", response_model=CheckOut)
async def start_check(check_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await checks.start(get_context(), db, check_id)


@router.post("/bgv-checks/{check_id}/result", response_model=CheckOut)
async def record_result(check_id: uuid.UUID, data: ResultIn, _: Manage, db: DB) -> Any:
    return await checks.record_result(get_context(), db, check_id, data)


@router.post("/bgv-checks/{check_id}/waive", response_model=CheckOut)
async def waive_check(check_id: uuid.UUID, data: WaiveIn, _: Adjudicate, db: DB) -> Any:
    return await checks.waive(get_context(), db, check_id, data)


@router.post("/bgv-checks/{check_id}/evidence", response_model=CheckOut)
async def upload_evidence(check_id: uuid.UUID, file: Annotated[UploadFile, File()], _: Manage, db: DB) -> Any:
    content = await read_upload(file, 10)
    return await checks.upload_evidence(get_context(), db, check_id, content, file.filename)


@router.get("/bgv-checks/{check_id}/packet")
async def check_packet(check_id: uuid.UUID, _: Manage, db: DB) -> Response:
    data, filename = await checks.packet(get_context(), db, check_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post(
    "/bgv-checks/{check_id}/discrepancies", response_model=DiscrepancyOut, status_code=status.HTTP_201_CREATED
)
async def raise_discrepancy(check_id: uuid.UUID, data: DiscrepancyIn, _: Manage, db: DB) -> Any:
    return await discrepancies.raise_for_check(get_context(), db, check_id, data)


@router.post("/bgv-discrepancies/{discrepancy_id}/resolve", response_model=DiscrepancyOut)
async def resolve_discrepancy(discrepancy_id: uuid.UUID, data: ResolveIn, _: Manage, db: DB) -> Any:
    return await discrepancies.resolve(get_context(), db, discrepancy_id, data)


# ---------------------------------------------------------------- reference checks
@router.post(
    "/candidates/{candidate_id}/reference-checks",
    response_model=ReferenceCheckOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_reference_check(
    candidate_id: uuid.UUID, data: ReferenceRequestIn, _: RefRequest, db: DB
) -> Any:
    return await references.request(get_context(), db, candidate_id, data)


@router.get("/candidates/{candidate_id}/reference-checks", response_model=list[ReferenceCheckOut])
async def candidate_reference_checks(candidate_id: uuid.UUID, _: RefRead, db: DB) -> Any:
    return await references.list_for_candidate(get_context(), db, candidate_id)


@router.get("/reference-checks/{reference_check_id}", response_model=ReferenceCheckOut)
async def get_reference_check(reference_check_id: uuid.UUID, _: RefRead, db: DB) -> Any:
    return await references.detail(get_context(), db, reference_check_id)


@router.post("/reference-checks/{reference_check_id}/review", response_model=ReferenceCheckOut)
async def review_reference_check(reference_check_id: uuid.UUID, data: ReviewIn, _: RefRequest, db: DB) -> Any:
    return await references.review(get_context(), db, reference_check_id, data)


@router.get("/reference-checks/{reference_check_id}/responses", response_model=ReferenceResponsesOut)
async def reference_responses(reference_check_id: uuid.UUID, _: Answers, db: DB) -> Any:
    return await references.responses(get_context(), db, reference_check_id)
