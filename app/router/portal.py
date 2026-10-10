"""Candidate Portal API (Epic 9 stories 9.3 / 9.4): ``/api/v1/portal``.

Every route depends on ``require_candidate`` (``tos_candidate`` cookie only; a user's ``tos_session`` gets
401) and is scoped to the signed-in candidate: another candidate's record is 404. Mutations need the CSRF
header and are limited to 60 / min per candidate session. Every response model is a ``Portal*`` allow-list.
Offer view / accept / decline routes are Epic 8's (``app/router/portal_offers.py``).
"""

import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.core import rate_limit
from app.core.context import get_context
from app.dependencies.candidate import CandidateDBDep, CandidateDep
from app.schemas.portal import (
    PortalApplication,
    PortalApplicationDetail,
    PortalConsent,
    PortalConsentGrantIn,
    PortalConsentWithdrawOut,
    PortalDataExport,
    PortalInterview,
    PortalMe,
    PortalPrivacyRequest,
    PortalPrivacyRequestIn,
    PortalProfile,
    PortalProfileUpdate,
    PortalResume,
    PortalScreening,
    PortalScreeningConsentIn,
    PortalScreeningDetail,
    PortalScreeningSubmitIn,
    PortalStatus,
    PortalWithdrawIn,
    PortalWithdrawReason,
)
from app.services import files_service
from app.services import portal as portal_service

WRITES_PER_MINUTE = 60


async def _write_limit(request: Request, principal: CandidateDep) -> None:
    if request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
        await rate_limit.enforce("portal_write", str(principal.session_id), WRITES_PER_MINUTE, 60)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    prefix="/portal", tags=["portal"], dependencies=[Depends(_write_limit), Depends(_no_store)]
)

svc = portal_service.self_service
privacy_svc = portal_service.privacy_self_service


def _ctx(principal: Any) -> Any:
    return principal.ctx(get_context())


# ================================================================ session
@router.get("/me", response_model=PortalMe)
async def me(principal: CandidateDep) -> PortalMe:
    return await portal_service.candidate_auth.me(principal)


@router.get("/csrf", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def reissue_csrf(principal: CandidateDep, request: Request) -> Response:  # noqa: ARG001
    """Epic 14 (FR-82): re-issue ``tos_csrf`` for the candidate session (the client retries once)."""
    from app.core.security import CANDIDATE_COOKIE, csrf_token_from_request, set_csrf_cookie
    from app.core.settings import get_settings

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    token = csrf_token_from_request(request, CANDIDATE_COOKIE)
    set_csrf_cookie(response, token, get_settings().candidate_session_absolute_hours * 3600)
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(principal: CandidateDep) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    await portal_service.candidate_auth.logout(principal, response)
    return response


# ================================================================ profile
@router.get("/profile", response_model=PortalProfile)
async def get_profile(principal: CandidateDep, db: CandidateDBDep) -> PortalProfile:
    return await svc.get_profile(principal, db)


@router.patch("/profile", response_model=PortalProfile)
async def update_profile(
    body: PortalProfileUpdate, principal: CandidateDep, db: CandidateDBDep
) -> PortalProfile:
    return await svc.update_profile(_ctx(principal), principal, db, body)


# ================================================================ resumes
@router.get("/resumes", response_model=list[PortalResume])
async def list_resumes(principal: CandidateDep, db: CandidateDBDep) -> list[PortalResume]:
    return await svc.list_resumes(principal, db)


@router.post("/resumes", response_model=PortalResume, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: Annotated[UploadFile, File()], principal: CandidateDep, db: CandidateDBDep
) -> PortalResume:
    data = await files_service.read_upload(file)
    return await svc.upload_resume(_ctx(principal), principal, db, data, file.filename)


@router.get(
    "/resumes/{resume_id}/download",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_resume(resume_id: uuid.UUID, principal: CandidateDep, db: CandidateDBDep) -> Response:
    result = await svc.download_resume(principal, db, resume_id)
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


# ================================================================ applications
@router.get("/applications", response_model=list[PortalApplication])
async def list_applications(principal: CandidateDep, db: CandidateDBDep) -> list[PortalApplication]:
    return await svc.list_applications(principal, db)


@router.get("/withdraw-reasons", response_model=list[PortalWithdrawReason])
async def withdraw_reasons(principal: CandidateDep, db: CandidateDBDep) -> list[PortalWithdrawReason]:  # noqa: ARG001
    return await svc.withdraw_reasons(db)


@router.get("/applications/{application_id}", response_model=PortalApplicationDetail)
async def get_application(
    application_id: uuid.UUID, principal: CandidateDep, db: CandidateDBDep
) -> PortalApplicationDetail:
    return await svc.get_application(principal, db, application_id)


@router.post("/applications/{application_id}/withdraw", response_model=PortalApplicationDetail)
async def withdraw_application(
    application_id: uuid.UUID, body: PortalWithdrawIn, principal: CandidateDep, db: CandidateDBDep
) -> PortalApplicationDetail:
    return await svc.withdraw(_ctx(principal), principal, db, application_id, body)


# ================================================================ interviews
@router.get("/interviews", response_model=list[PortalInterview])
async def list_interviews(
    principal: CandidateDep, db: CandidateDBDep, upcoming: Annotated[bool, Query()] = True
) -> list[PortalInterview]:
    return await svc.list_interviews(_ctx(principal), principal, db, upcoming=upcoming)


# ================================================================ AI screenings
@router.get("/ai-screenings", response_model=list[PortalScreening])
async def list_screenings(principal: CandidateDep, db: CandidateDBDep) -> list[PortalScreening]:
    return await svc.list_screenings(principal, db)


@router.get("/ai-screenings/{screening_id}", response_model=PortalScreeningDetail)
async def get_screening(
    screening_id: uuid.UUID, principal: CandidateDep, db: CandidateDBDep
) -> PortalScreeningDetail:
    return await svc.get_screening(principal, db, screening_id)


@router.post("/ai-screenings/{screening_id}/consent", response_model=PortalStatus)
async def screening_consent(
    screening_id: uuid.UUID, body: PortalScreeningConsentIn, principal: CandidateDep, db: CandidateDBDep
) -> PortalStatus:
    return await svc.screening_consent(_ctx(principal), principal, db, screening_id, body)


@router.post("/ai-screenings/{screening_id}/submit", response_model=PortalStatus)
async def screening_submit(
    screening_id: uuid.UUID, body: PortalScreeningSubmitIn, principal: CandidateDep, db: CandidateDBDep
) -> PortalStatus:
    return await svc.screening_submit(_ctx(principal), principal, db, screening_id, body.answers)


# ================================================================ privacy
@router.get("/consents", response_model=list[PortalConsent])
async def list_consents(principal: CandidateDep, db: CandidateDBDep) -> list[PortalConsent]:
    return await privacy_svc.list_consents(principal, db)


@router.post("/consents", response_model=PortalConsent, status_code=status.HTTP_201_CREATED)
async def grant_consent(
    body: PortalConsentGrantIn, principal: CandidateDep, db: CandidateDBDep
) -> PortalConsent:
    return await privacy_svc.grant_consent(_ctx(principal), principal, db, body)


@router.post("/consents/{consent_id}/withdraw", response_model=PortalConsentWithdrawOut)
async def withdraw_consent(
    consent_id: uuid.UUID, principal: CandidateDep, db: CandidateDBDep
) -> PortalConsentWithdrawOut:
    return await privacy_svc.withdraw_consent(_ctx(principal), principal, db, consent_id)


@router.post("/data-exports", response_model=PortalDataExport, status_code=status.HTTP_202_ACCEPTED)
async def request_export(principal: CandidateDep, db: CandidateDBDep) -> PortalDataExport:
    return await privacy_svc.request_export(_ctx(principal), principal, db)


@router.get("/data-exports", response_model=list[PortalDataExport])
async def list_exports(principal: CandidateDep, db: CandidateDBDep) -> list[PortalDataExport]:
    return await privacy_svc.list_exports(principal, db)


@router.get(
    "/data-exports/{job_id}/download",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def download_export(job_id: uuid.UUID, principal: CandidateDep, db: CandidateDBDep) -> Response:
    content, filename = await privacy_svc.download_export(principal, db, job_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/privacy-requests", response_model=PortalPrivacyRequest, status_code=status.HTTP_201_CREATED)
async def create_privacy_request(
    body: PortalPrivacyRequestIn, principal: CandidateDep, db: CandidateDBDep
) -> PortalPrivacyRequest:
    return await privacy_svc.create_request(_ctx(principal), principal, db, body)


@router.get("/privacy-requests", response_model=list[PortalPrivacyRequest])
async def list_privacy_requests(principal: CandidateDep, db: CandidateDBDep) -> list[PortalPrivacyRequest]:
    return await privacy_svc.list_requests(principal, db)
