"""Offer endpoints (Epic 8): offers, versions, approval submit, release, withdraw, letters, templates and
settings. Each endpoint calls one service function."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse, Response

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.offers import (
    MergeFieldOut,
    OfferCreate,
    OfferDetailOut,
    OfferOut,
    OfferSettingsOut,
    OfferSettingsUpdate,
    OfferTemplateIn,
    OfferTemplateOut,
    OfferTemplateUpdate,
    OfferUpdate,
    OfferVersionOut,
    TemplatePreviewIn,
    WithdrawIn,
)
from app.services import offers_service as svc

router = APIRouter(prefix="/offers", tags=["offers"])
applications_router = APIRouter(prefix="/applications", tags=["offers"])
demands_router = APIRouter(prefix="/demands", tags=["offers"])
templates_router = APIRouter(prefix="/offer-templates", tags=["offers"])

Read = Annotated[Principal, Depends(require("offer:read"))]
Write = Annotated[Principal, Depends(require("offer:write"))]
Release = Annotated[Principal, Depends(require("offer:release"))]
Withdraw = Annotated[Principal, Depends(require("offer:withdraw"))]
Manage = Annotated[Principal, Depends(require("offer_template:manage"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep
PDF_HEADERS = {"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="offer-preview.pdf"'}


# ================================================================ settings (before /{offer_id})
@router.get("/settings", response_model=OfferSettingsOut)
async def get_settings(_: Manage, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.patch("/settings", response_model=OfferSettingsOut)
async def update_settings(body: OfferSettingsUpdate, _: Manage, db: DB) -> Any:
    return await svc.update_settings(get_context(), db, body)


# ================================================================ offers
@router.get("", response_model=Page[OfferOut])
async def list_offers(
    _: Read,
    db: DB,
    params: Paging,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    owner: str | None = None,
) -> Any:
    return await svc.list_offers(get_context(), db, params, status=status_, owner=owner)


@router.get("/{offer_id}", response_model=OfferDetailOut)
async def get_offer(offer_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_offer(get_context(), db, offer_id)


@router.patch("/{offer_id}", response_model=OfferDetailOut)
async def update_offer(offer_id: uuid.UUID, body: OfferUpdate, _: Write, db: DB) -> Any:
    return await svc.update_offer(get_context(), db, offer_id, body)


@router.post("/{offer_id}/submit", response_model=OfferDetailOut)
async def submit_offer(offer_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await svc.submit_offer(get_context(), db, offer_id)


@router.post("/{offer_id}/release", response_model=OfferDetailOut)
async def release_offer(offer_id: uuid.UUID, _: Release, db: DB) -> Any:
    return await svc.release(get_context(), db, offer_id)


@router.post("/{offer_id}/withdraw", response_model=OfferDetailOut)
async def withdraw_offer(offer_id: uuid.UUID, body: WithdrawIn, _: Withdraw, db: DB) -> Any:
    return await svc.withdraw_offer(get_context(), db, offer_id, body.reason)


@router.get("/{offer_id}/versions/{version_no}", response_model=OfferVersionOut)
async def get_version(offer_id: uuid.UUID, version_no: int, _: Read, db: DB) -> Any:
    return await svc.get_version(get_context(), db, offer_id, version_no)


@router.post("/{offer_id}/letter/preview", response_class=Response)
async def letter_preview(offer_id: uuid.UUID, _: Read, db: DB) -> Response:
    pdf = await svc.offers.letter_preview(get_context(), db, offer_id)
    return Response(content=pdf, media_type="application/pdf", headers=PDF_HEADERS)


@router.get("/{offer_id}/letter", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def letter(offer_id: uuid.UUID, _: Read, db: DB, version_no: int | None = None) -> RedirectResponse:
    file_id = await svc.offers.letter_file_id(get_context(), db, offer_id, version_no)
    return RedirectResponse(f"/api/v1/files/{file_id}/download", status_code=status.HTTP_302_FOUND)


@router.get("/{offer_id}/accepted-letter", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def accepted_letter(offer_id: uuid.UUID, _: Read, db: DB) -> RedirectResponse:
    file_id = await svc.offers.accepted_letter_file_id(get_context(), db, offer_id)
    return RedirectResponse(f"/api/v1/files/{file_id}/download", status_code=status.HTTP_302_FOUND)


# ================================================================ application / demand scoped
@applications_router.post(
    "/{application_id}/offers", response_model=OfferDetailOut, status_code=status.HTTP_201_CREATED
)
async def create_offer(application_id: uuid.UUID, body: OfferCreate, _: Write, db: DB) -> Any:
    return await svc.create_offer(get_context(), db, application_id, body)


@applications_router.get("/{application_id}/offers", response_model=list[OfferOut])
async def application_offers(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_for_application(get_context(), db, application_id)


@demands_router.get("/{demand_id}/offers", response_model=list[OfferOut])
async def demand_offers(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_for_demand(get_context(), db, demand_id)


# ================================================================ templates
@templates_router.get("/merge-fields", response_model=list[MergeFieldOut])
async def merge_fields(_: Read) -> Any:
    return svc.templates.merge_fields()


@templates_router.post("/preview", response_class=Response)
async def preview_template(body: TemplatePreviewIn, _: Manage) -> Response:
    pdf = svc.templates_preview(body.body_md)
    return Response(content=pdf, media_type="application/pdf", headers=PDF_HEADERS)


@templates_router.get("", response_model=list[OfferTemplateOut])
async def list_templates(_: Read, db: DB, include_archived: bool = False) -> Any:
    return await svc.templates.list_templates(get_context(), db, include_archived)


@templates_router.post("", response_model=OfferTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: OfferTemplateIn, _: Manage, db: DB) -> Any:
    return await svc.templates.create_template(get_context(), db, body)


@templates_router.patch("/{template_id}", response_model=OfferTemplateOut)
async def update_template(template_id: uuid.UUID, body: OfferTemplateUpdate, _: Manage, db: DB) -> Any:
    return await svc.templates.update_template(get_context(), db, template_id, body)


@templates_router.post("/{template_id}/archive", response_model=OfferTemplateOut)
async def archive_template(template_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.templates.archive_template(get_context(), db, template_id)
