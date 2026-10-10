# ruff: noqa: E501
"""Employee referrals (Epic 42, FR-213..FR-216, AD-50): ``/api/v1``.

* ``/referral-programs``, ``/referral-settings`` -- ``referral:configure``.
* ``/me/referrals/*`` -- **self-service, no permission key**: any active internal User refers and sees their own
  referrals (P1 self-service pattern, like ``/me/notifications``); program eligibility is the gate. Output is the
  referrer-safe schema only (four status labels, own reward).
* ``/referrals`` -- ``referral:read`` (data-scoped through the Demand); ``link-demand`` -- ``referral:manage``.
* ``/referral-rewards`` -- list (``referral:read``), void / mark paid (``referral:manage``), payout export
  (``referral:manage`` + ``report:export``). Approval is on the approval engine (``/approvals``; human only).
* The public referee page is ``router/public_referral.py``.

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from pydantic import EmailStr

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.referrals import (
    CandidateReferralOut,
    LinkDemandIn,
    MarkPaidIn,
    MarkPaidOut,
    ProgramIn,
    ProgramOut,
    ProgramPatch,
    ReferIn,
    ReferralOut,
    ReferralPage,
    RewardOut,
    SettingsIn,
    SettingsOut,
    ShareLinkIn,
    VoidIn,
)
from app.schemas.referrals_referrer import ReferrerOpeningOut, ReferrerReferralOut, ReferrerShareLinkOut
from app.services import files_service
from app.services.referrals import acceptance, payouts, portal, programs, rewards, tracking
from app.services.referrals.common import MAX_RESUME_MB

router = APIRouter(tags=["referrals"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("referral:read"))]
Manage = Annotated[Principal, Depends(require("referral:manage"))]
Configure = Annotated[Principal, Depends(require("referral:configure"))]


async def _resume(file: UploadFile) -> tuple[bytes, str | None]:
    return await files_service.read_upload(file, MAX_RESUME_MB), file.filename


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


ReferForm = Annotated[ReferIn, Depends(_refer_form)]


# ================================================================ programs / settings
@router.get("/referral-programs", response_model=list[ProgramOut])
async def list_programs(_: Configure, db: DB, include_archived: bool = False) -> Any:
    return await programs.list_programs(get_context(), db, include_archived=include_archived)


@router.post("/referral-programs", response_model=ProgramOut, status_code=status.HTTP_201_CREATED)
async def create_program(body: ProgramIn, _: Configure, db: DB) -> Any:
    return await programs.create_program(get_context(), db, body)


@router.get("/referral-programs/{program_id}", response_model=ProgramOut)
async def get_program(program_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await programs.get_program(get_context(), db, program_id)


@router.patch("/referral-programs/{program_id}", response_model=ProgramOut)
async def update_program(program_id: uuid.UUID, body: ProgramPatch, _: Configure, db: DB) -> Any:
    return await programs.update_program(get_context(), db, program_id, body)


@router.post("/referral-programs/{program_id}/archive", response_model=ProgramOut)
async def archive_program(program_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await programs.archive_program(get_context(), db, program_id)


@router.get("/referral-settings", response_model=SettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await programs.get_settings(get_context(), db)


@router.put("/referral-settings", response_model=SettingsOut)
async def put_settings(body: SettingsIn, _: Configure, db: DB) -> Any:
    return await programs.put_settings(get_context(), db, body)


# ================================================================ self-service (no permission key)
@router.get("/me/referrals", response_model=list[ReferrerReferralOut])
async def my_referrals(principal: CurrentUserDep, db: DB) -> Any:
    return await portal.user_list(principal, db)


@router.get("/me/referrals/openings", response_model=list[ReferrerOpeningOut])
async def my_openings(principal: CurrentUserDep, db: DB) -> Any:
    return await portal.user_openings(principal, db)


@router.post("/me/referrals", response_model=ReferrerReferralOut, status_code=status.HTTP_201_CREATED)
async def refer(
    principal: CurrentUserDep, db: DB, form: ReferForm, resume: Annotated[UploadFile, File()]
) -> Any:
    data, name = await _resume(resume)
    return await portal.user_refer(get_context(), principal, db, form, data, name)


@router.post("/me/referrals/share-links", response_model=ReferrerShareLinkOut)
async def my_share_link(body: ShareLinkIn, principal: CurrentUserDep, db: DB) -> Any:
    return await portal.user_share_link(principal, db, body.demand_id)


@router.post("/me/referrals/{referral_id}/withdraw", response_model=ReferrerReferralOut)
async def withdraw(referral_id: uuid.UUID, principal: CurrentUserDep, db: DB) -> Any:
    return await portal.user_withdraw(principal, db, referral_id)


# ================================================================ internal views
@router.get("/referrals", response_model=ReferralPage)
async def list_referrals(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    demand_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    channel: Literal["upload", "share_link"] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Any:
    return await tracking.list_referrals(
        get_context(),
        db,
        status=status_,
        demand_id=demand_id,
        program_id=program_id,
        channel=channel,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )


@router.get("/referrals/by-candidate/{candidate_id}", response_model=list[CandidateReferralOut])
async def by_candidate(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await tracking.for_candidate(get_context(), db, candidate_id)


@router.get("/referrals/{referral_id}", response_model=ReferralOut)
async def get_referral(referral_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await tracking.get_referral(get_context(), db, referral_id)


@router.get("/referrals/{referral_id}/resume")
async def referral_resume(referral_id: uuid.UUID, _: Read, db: DB) -> Response:
    file = await tracking.resume_download(get_context(), db, referral_id)
    data = await files_service.read_file(file)
    return Response(
        content=data,
        media_type=file.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file.original_name or "resume"}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/referrals/{referral_id}/link-demand", response_model=ReferralOut)
async def link_demand(referral_id: uuid.UUID, body: LinkDemandIn, _: Manage, db: DB) -> Any:
    return await acceptance.link_demand(get_context(), db, referral_id, body.demand_id)


# ================================================================ rewards
@router.get("/referral-rewards", response_model=list[RewardOut])
async def list_rewards(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    channel: Literal["payroll", "payout_export"] | None = None,
) -> Any:
    return await rewards.list_rewards(get_context(), db, status=status_, channel=channel)


@router.get("/referral-rewards/payout-export")
async def payout_export(
    _: Manage,
    db: DB,
    status_: Annotated[Literal["approved", "paid"], Query(alias="status")] = "approved",
    channel: Literal["payroll", "payout_export"] = "payout_export",
    format: Literal["xlsx", "csv"] = "xlsx",
) -> Response:
    content, media, filename = await payouts.payout_export(
        get_context(), db, status=status_, channel=channel, fmt=format
    )
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/referral-rewards/mark-paid", response_model=MarkPaidOut)
async def mark_paid(body: MarkPaidIn, _: Manage, db: DB) -> Any:
    return await payouts.mark_paid(get_context(), db, body.ids, body.paid_on, body.payout_ref)


@router.post("/referral-rewards/{reward_id}/void", response_model=RewardOut)
async def void_reward(reward_id: uuid.UUID, body: VoidIn, _: Manage, db: DB) -> Any:
    return await rewards.void_manual(get_context(), db, reward_id, body.reason)
