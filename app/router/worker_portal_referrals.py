# ruff: noqa: E501
"""Worker "My Work" referrals (Epic 42, ``/api/v1/portal/work/referrals``): openings to refer for, refer a friend
(resume upload), personal share links and My referrals. Any Worker (a Candidate with a Placement); referrer-safe
payloads only (four status labels, own reward). The list runs in the Worker's org-scoped session (another Worker's
referrals are invisible -> 404); JSON responses are scanned for worker-forbidden fields outside production."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import EmailStr

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.referrals import ReferIn, ShareLinkIn
from app.schemas.referrals_referrer import ReferrerOpeningOut, ReferrerReferralOut, ReferrerShareLinkOut
from app.services import files_service
from app.services.referrals import portal
from app.services.referrals.common import MAX_RESUME_MB

router = APIRouter(
    prefix="/portal/work/referrals", tags=["portal-work"], route_class=portal_guard_route("worker")
)


def _refer_form(
    referee_name: Annotated[str, Form(min_length=1, max_length=200)],
    referee_email: Annotated[EmailStr, Form()],
    demand_id: Annotated[uuid.UUID | None, Form()] = None,
    referee_phone: Annotated[str | None, Form(max_length=40)] = None,
    relationship: Annotated[str | None, Form(max_length=100)] = None,
    note: Annotated[str | None, Form(max_length=2000)] = None,
) -> ReferIn:
    return ReferIn(
        demand_id=demand_id,
        referee_name=referee_name,
        referee_email=referee_email,
        referee_phone=referee_phone or None,
        relationship=relationship or None,
        note=note or None,
    )


@router.get("", response_model=list[ReferrerReferralOut])
async def my_referrals(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_list(principal, db)


@router.get("/openings", response_model=list[ReferrerOpeningOut])
async def openings(principal: WorkerDep) -> Any:
    return await portal.worker_openings(principal)


@router.post("", response_model=ReferrerReferralOut, status_code=status.HTTP_201_CREATED)
async def refer(
    principal: WorkerDep,
    form: Annotated[ReferIn, Depends(_refer_form)],
    resume: Annotated[UploadFile, File()],
) -> Any:
    data = await files_service.read_upload(resume, MAX_RESUME_MB)
    return await portal.worker_refer(principal, form, data, resume.filename, get_context().ip)


@router.post("/share-links", response_model=ReferrerShareLinkOut)
async def share_link(body: ShareLinkIn, principal: WorkerDep) -> Any:
    return await portal.worker_share_link(principal, body.demand_id)


@router.post("/{referral_id}/withdraw", response_model=ReferrerReferralOut)
async def withdraw(referral_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_withdraw(principal, db, referral_id)
