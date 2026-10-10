"""Career-site administration (Epic 9 story 9.1): ``/api/v1/admin/career-site`` (``career_site:configure``),
``PATCH /api/v1/demands/{id}/posting`` (apply questions; Epic 4 ``demand:update``) and the DSR intake list
``GET /api/v1/privacy/dsr-requests`` (``dsr:read``; Epic 12's ``/privacy/dsrs`` is the full DSR API)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.portal import CareerSiteOut, CareerSiteUpdate, PostingQuestionsIn, PostingQuestionsOut
from app.schemas.privacy import DsrFilters, DsrOut
from app.services import files_service, privacy
from app.services import portal as portal_service

router = APIRouter(prefix="/admin/career-site", tags=["career-site"])
postings_router = APIRouter(tags=["career-site"])

Configure = Annotated[Principal, Depends(require("career_site:configure"))]
DemandUpdate = Annotated[Principal, Depends(require("demand:update"))]
DsrRead = Annotated[Principal, Depends(require("dsr:read"))]
Paging = Annotated[PageParams, Depends()]

site = portal_service.site


@router.get("", response_model=CareerSiteOut)
async def get_career_site(_: Configure, db: TenantDBSessionDep) -> CareerSiteOut:
    return await site.get_site(get_context(), db)


@router.patch("", response_model=CareerSiteOut)
async def update_career_site(body: CareerSiteUpdate, _: Configure, db: TenantDBSessionDep) -> CareerSiteOut:
    return await site.update_site(get_context(), db, body)


@router.post("/logo", response_model=CareerSiteOut)
async def upload_logo(
    file: Annotated[UploadFile, File()], _: Configure, db: TenantDBSessionDep
) -> CareerSiteOut:
    data = await files_service.read_upload(file, site.MAX_IMAGE_MB)
    return await site.upload_image(get_context(), db, "logo", data, file.filename)


@router.post("/header", response_model=CareerSiteOut)
async def upload_header(
    file: Annotated[UploadFile, File()], _: Configure, db: TenantDBSessionDep
) -> CareerSiteOut:
    data = await files_service.read_upload(file, site.MAX_IMAGE_MB)
    return await site.upload_image(get_context(), db, "header", data, file.filename)


@postings_router.patch("/demands/{demand_id}/posting", response_model=PostingQuestionsOut)
async def update_posting_questions(
    demand_id: uuid.UUID, body: PostingQuestionsIn, _: DemandUpdate, db: TenantDBSessionDep
) -> PostingQuestionsOut:
    return await site.set_apply_questions(get_context(), db, demand_id, body)


@postings_router.get("/privacy/dsr-requests", response_model=Page[DsrOut])
async def list_dsr_requests(
    _: DsrRead, db: TenantDBSessionDep, filters: Annotated[DsrFilters, Depends()], params: Paging
) -> Page[DsrOut]:
    return await privacy.dsr.list_dsrs(get_context(), db, filters, params)
