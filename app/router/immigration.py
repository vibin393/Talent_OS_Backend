# ruff: file-ignore[line-too-long]
"""Immigration (Epic 46, FR-232..FR-236, AD-52): ``/api/v1/immigration`` and the public counsel / Candidate upload
page ``/api/v1/public/links/{token}/immigration-upload`` (purpose ``immigration_upload``).

Permissions: ``immigration:read`` (lists, detail, dashboard; data-scoped), ``immigration:manage`` (case writes,
checklist, review; data-scoped), ``immigration:configure`` (visa types, counsel). Unmask additionally needs
``identity:view`` (audited). Approved / Refused are human only (403 ``human_only_action``). Each endpoint calls
one service function.
"""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, Response, UploadFile, status

from app.core import rate_limit
from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.immigration import (
    AdoptIn,
    AtRiskOut,
    CaseCreate,
    CaseEventOut,
    CaseOut,
    CasePatch,
    CaseSummaryOut,
    CounselIn,
    CounselOut,
    CounselPatch,
    DashboardOut,
    DefinitionIn,
    DependantIn,
    DependantOut,
    DependantPatch,
    KeyDateIn,
    OutcomeIn,
    PermitIn,
    PermitOut,
    ReasonIn,
    ReceiptIn,
    RequestOut,
    SendIn,
    SendOut,
    StageMoveIn,
    UnmaskOut,
    VerifyOut,
    VisaTemplateOut,
    VisaTypeCreate,
    VisaTypeDetailOut,
    VisaTypeOut,
    VisaTypePatch,
    WithdrawIn,
)
from app.schemas.immigration_portal import PublicUploadOut, PublicUploadViewOut
from app.services import files_service
from app.services.immigration import cases, checklist, counsel, dashboard, permits, public, visa_types

router = APIRouter(prefix="/immigration", tags=["immigration"])
public_router = APIRouter(prefix="/public/links", tags=["public"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("immigration:read"))]
Manage = Annotated[Principal, Depends(require("immigration:manage"))]
Configure = Annotated[Principal, Depends(require("immigration:configure"))]
NO_STORE = {"Cache-Control": "no-store"}


async def _upload(file: UploadFile) -> tuple[bytes, str | None]:
    return await files_service.read_upload(file, checklist.MAX_UPLOAD_MB), file.filename


# ================================================================ visa types / templates
@router.get("/visa-templates", response_model=list[VisaTemplateOut])
async def list_templates(_: Read, db: DB) -> Any:
    return await visa_types.list_templates(get_context(), db)


@router.post(
    "/visa-templates/{key}/adopt", response_model=VisaTypeDetailOut, status_code=status.HTTP_201_CREATED
)
async def adopt_template(key: str, body: AdoptIn, _: Configure, db: DB) -> Any:
    return await visa_types.adopt(get_context(), db, key, body)


@router.get("/visa-types", response_model=list[VisaTypeOut])
async def list_visa_types(_: Read, db: DB, include_archived: bool = False) -> Any:
    return await visa_types.list_types(get_context(), db, include_archived=include_archived)


@router.post("/visa-types", response_model=VisaTypeDetailOut, status_code=status.HTTP_201_CREATED)
async def create_visa_type(body: VisaTypeCreate, _: Configure, db: DB) -> Any:
    return await visa_types.create(get_context(), db, body)


@router.get("/visa-types/{visa_type_id}", response_model=VisaTypeDetailOut)
async def get_visa_type(visa_type_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await visa_types.detail(get_context(), db, visa_type_id)


@router.put("/visa-types/{visa_type_id}/definition", response_model=VisaTypeDetailOut)
async def put_definition(visa_type_id: uuid.UUID, body: DefinitionIn, _: Configure, db: DB) -> Any:
    return await visa_types.update_definition(get_context(), db, visa_type_id, body.definition)


@router.patch("/visa-types/{visa_type_id}", response_model=VisaTypeDetailOut)
async def patch_visa_type(visa_type_id: uuid.UUID, body: VisaTypePatch, _: Configure, db: DB) -> Any:
    return await visa_types.patch(get_context(), db, visa_type_id, body)


@router.post("/visa-types/{visa_type_id}/archive", response_model=VisaTypeDetailOut)
async def archive_visa_type(visa_type_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await visa_types.archive(get_context(), db, visa_type_id)


# ================================================================ counsel
@router.get("/counsel", response_model=list[CounselOut])
async def list_counsel(_: Read, db: DB, include_archived: bool = False) -> Any:
    return await counsel.list_counsel(get_context(), db, include_archived=include_archived)


@router.post("/counsel", response_model=CounselOut, status_code=status.HTTP_201_CREATED)
async def create_counsel(body: CounselIn, _: Configure, db: DB) -> Any:
    return await counsel.create(get_context(), db, body)


@router.patch("/counsel/{counsel_id}", response_model=CounselOut)
async def update_counsel(counsel_id: uuid.UUID, body: CounselPatch, _: Configure, db: DB) -> Any:
    return await counsel.update(get_context(), db, counsel_id, body)


# ================================================================ dashboard
@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(_: Read, db: DB) -> Any:
    return await dashboard.summary(get_context(), db)


@router.get("/dashboard/at-risk", response_model=list[AtRiskOut])
async def get_at_risk(_: Read, db: DB) -> Any:
    return await dashboard.at_risk(get_context(), db)


# ================================================================ cases
@router.get("/cases", response_model=list[CaseSummaryOut])
async def list_cases(
    _: Read,
    db: DB,
    status: str | None = None,
    stage: str | None = None,
    stage_category: str | None = None,
    country: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    visa_type_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    overdue: bool = False,
    permits_expiring_days: int | None = None,
    q: str | None = None,
) -> Any:
    return await cases.list_cases(
        get_context(),
        db,
        status=status,
        stage=stage,
        stage_category=stage_category,
        country=country,
        owner_user_id=owner_user_id,
        visa_type_id=visa_type_id,
        candidate_id=candidate_id,
        overdue_only=overdue,
        permits_expiring_days=permits_expiring_days,
        q=q,
    )


@router.post("/cases", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
async def create_case(body: CaseCreate, _: Manage, db: DB) -> Any:
    return await cases.create(get_context(), db, body)


@router.get("/cases/{case_id}", response_model=CaseOut)
async def get_case(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.get(get_context(), db, case_id)


@router.patch("/cases/{case_id}", response_model=CaseOut)
async def patch_case(case_id: uuid.UUID, body: CasePatch, _: Manage, db: DB) -> Any:
    return await cases.patch(get_context(), db, case_id, body)


@router.post("/cases/{case_id}/stage", response_model=CaseOut)
async def move_stage(case_id: uuid.UUID, body: StageMoveIn, _: Manage, db: DB) -> Any:
    return await cases.move_stage(get_context(), db, case_id, body)


@router.post("/cases/{case_id}/key-dates", response_model=CaseOut)
async def set_key_date(case_id: uuid.UUID, body: KeyDateIn, _: Manage, db: DB) -> Any:
    return await cases.set_key_date(get_context(), db, case_id, body)


@router.post("/cases/{case_id}/receipt", response_model=CaseOut)
async def set_receipt(case_id: uuid.UUID, body: ReceiptIn, _: Manage, db: DB) -> Any:
    return await cases.set_receipt(get_context(), db, case_id, body.number)


@router.post("/cases/{case_id}/receipt/unmask", response_model=UnmaskOut)
async def unmask_receipt(case_id: uuid.UUID, _: Read, db: DB, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return UnmaskOut(number=await cases.unmask_receipt(get_context(), db, case_id))


@router.post("/cases/{case_id}/outcome", response_model=CaseOut)
async def record_outcome(case_id: uuid.UUID, body: OutcomeIn, _: Manage, db: DB) -> Any:
    return await cases.record_outcome(get_context(), db, case_id, body)


@router.post("/cases/{case_id}/withdraw", response_model=CaseOut)
async def withdraw_case(case_id: uuid.UUID, body: WithdrawIn, _: Manage, db: DB) -> Any:
    return await cases.withdraw(get_context(), db, case_id, body)


@router.get("/cases/{case_id}/timeline", response_model=list[CaseEventOut])
async def case_timeline(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.timeline(get_context(), db, case_id)


# ---------------------------------------------------------------- dependants
@router.get("/cases/{case_id}/dependants", response_model=list[DependantOut])
async def list_dependants(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.list_dependants(get_context(), db, case_id)


@router.post("/cases/{case_id}/dependants", response_model=DependantOut, status_code=status.HTTP_201_CREATED)
async def add_dependant(case_id: uuid.UUID, body: DependantIn, _: Manage, db: DB) -> Any:
    return await cases.add_dependant(get_context(), db, case_id, body)


@router.patch("/cases/{case_id}/dependants/{dependant_id}", response_model=DependantOut)
async def update_dependant(
    case_id: uuid.UUID, dependant_id: uuid.UUID, body: DependantPatch, _: Manage, db: DB
) -> Any:
    return await cases.update_dependant(get_context(), db, case_id, dependant_id, body)


@router.delete("/cases/{case_id}/dependants/{dependant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dependant(case_id: uuid.UUID, dependant_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await cases.delete_dependant(get_context(), db, case_id, dependant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/cases/{case_id}/dependants/{dependant_id}/passport/unmask", response_model=UnmaskOut)
async def unmask_passport(
    case_id: uuid.UUID, dependant_id: uuid.UUID, _: Read, db: DB, response: Response
) -> Any:
    response.headers.update(NO_STORE)
    return UnmaskOut(number=await cases.unmask_passport(get_context(), db, case_id, dependant_id))


# ---------------------------------------------------------------- checklist
@router.get("/cases/{case_id}/requests", response_model=list[RequestOut])
async def list_requests(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await checklist.list_requests(get_context(), db, case_id)


@router.post("/cases/{case_id}/checklist/generate", response_model=list[RequestOut])
async def generate_checklist(case_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await checklist.generate(get_context(), db, case_id)


@router.post("/cases/{case_id}/requests/send", response_model=SendOut)
async def send_requests(case_id: uuid.UUID, body: SendIn, _: Manage, db: DB) -> Any:
    return await checklist.send(get_context(), db, case_id, body.request_ids, body.due_date)


@router.post("/requests/{request_id}/upload", response_model=RequestOut)
async def upload_request(
    request_id: uuid.UUID, _: Manage, db: DB, file: Annotated[UploadFile, File()]
) -> Any:
    data, name = await _upload(file)
    return await checklist.upload_internal(get_context(), db, request_id, data, name)


@router.post("/requests/{request_id}/verify", response_model=VerifyOut)
async def verify_request(request_id: uuid.UUID, _: Manage, db: DB) -> Any:
    out, warnings = await checklist.verify(get_context(), db, request_id)
    return VerifyOut(request=out, warnings=warnings)


@router.post("/requests/{request_id}/reject", response_model=RequestOut)
async def reject_request(request_id: uuid.UUID, body: ReasonIn, _: Manage, db: DB) -> Any:
    return await checklist.reject(get_context(), db, request_id, body.reason)


@router.post("/requests/{request_id}/waive", response_model=RequestOut)
async def waive_request(request_id: uuid.UUID, body: ReasonIn, _: Manage, db: DB) -> Any:
    return await checklist.waive(get_context(), db, request_id, body.reason)


# ---------------------------------------------------------------- permits
@router.get("/cases/{case_id}/permits", response_model=list[PermitOut])
async def list_permits(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await permits.list_for_case(get_context(), db, case_id)


@router.post("/cases/{case_id}/permits", response_model=PermitOut, status_code=status.HTTP_201_CREATED)
async def record_permit(case_id: uuid.UUID, body: PermitIn, _: Manage, db: DB) -> Any:
    return await permits.record_amendment(get_context(), db, case_id, body)


@router.post("/permits/{permit_id}/unmask", response_model=UnmaskOut)
async def unmask_permit(permit_id: uuid.UUID, _: Read, db: DB, response: Response) -> Any:
    response.headers.update(NO_STORE)
    return UnmaskOut(number=await permits.unmask(get_context(), db, permit_id))


# ================================================================ public upload page (purpose immigration_upload)
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


async def _public_guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("immigration_upload_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 30, 3600)
    return token


PublicToken = Annotated[str, Depends(_public_guard)]


@public_router.get("/{token}/immigration-upload", response_model=PublicUploadViewOut)
async def public_upload_view(token: PublicToken) -> Any:
    return await public.view(token)


@public_router.post("/{token}/immigration-upload", response_model=PublicUploadOut)
async def public_upload(
    token: PublicToken,
    item_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    consent: Annotated[bool, Form()] = False,
) -> Any:
    data, name = await _upload(file)
    return await public.upload(token, item_id, consent, data, name)
