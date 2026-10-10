"""Worker "My Work" signatures (Epic 22, ``/api/v1/portal/work/signatures``) and the Candidate Portal offer
acceptance code (``/api/v1/portal/offers/{id}/otp``). The Worker sees only their own signer rows (worker org
scope + explicit ownership; 404 otherwise) and signs through the shared ceremony (channel ``portal``)."""

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_context
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.documents import CeremonyActionOut, CeremonyOut, ConsentIn, DeclineSignIn, OtpSentOut, SignIn
from app.schemas.documents_portal import OfferOtpStatusOut, PortalSignatureOut
from app.services.documents import portal
from app.services.documents.esign import SignerSession

router = APIRouter(prefix="/portal/work/signatures", tags=["portal-work"])
offers_router = APIRouter(prefix="/portal/offers", tags=["portal"])


async def _session(principal: Any, db: AsyncSession, signer_id: uuid.UUID, request: Request) -> SignerSession:
    await portal.worker_signer(db, principal.candidate_id, signer_id)
    return SignerSession(
        db, signer_id, "portal", get_context().ip, request.headers.get("user-agent"), actor_kind="candidate"
    )


@router.get("", response_model=list[PortalSignatureOut])
async def list_my_signatures(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_for_worker(db, principal.candidate_id)


@router.get("/{signer_id}", response_model=CeremonyOut)
async def my_ceremony(
    signer_id: uuid.UUID, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    session = await _session(principal, db, signer_id, request)
    return await session.ceremony(document_url=f"/api/v1/portal/work/signatures/{signer_id}/view")


@router.get("/{signer_id}/view", response_class=Response)
async def my_document(
    signer_id: uuid.UUID, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Response:
    session = await _session(principal, db, signer_id, request)
    data, name = await session.view()
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{signer_id}/otp", response_model=OtpSentOut)
async def my_otp(signer_id: uuid.UUID, request: Request, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    expires, hint = await (await _session(principal, db, signer_id, request)).request_otp()
    return OtpSentOut(sent=True, expires_at=expires, email_hint=hint)


@router.post("/{signer_id}/consent", response_model=CeremonyActionOut)
async def my_consent(
    signer_id: uuid.UUID, body: ConsentIn, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    await (await _session(principal, db, signer_id, request)).consent(body.agreed)
    return CeremonyActionOut(status="consented", request_status="open")


@router.post("/{signer_id}/sign", response_model=CeremonyActionOut)
async def my_sign(
    signer_id: uuid.UUID, body: SignIn, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    session = await _session(principal, db, signer_id, request)
    req = await session.sign(body.typed_name, body.otp, agreed=body.agreed)
    return CeremonyActionOut(status="signed", request_status=req.status)


@router.post("/{signer_id}/decline", response_model=CeremonyActionOut)
async def my_decline(
    signer_id: uuid.UUID, body: DeclineSignIn, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    req = await (await _session(principal, db, signer_id, request)).decline(body.reason)
    return CeremonyActionOut(status="declined", request_status=req.status)


# ---------------------------------------------------------------- Candidate Portal offers (Epic 8 + 22)
@offers_router.get("/{offer_id}/otp", response_model=OfferOtpStatusOut)
async def offer_otp_status(offer_id: uuid.UUID, principal: WorkerDep) -> Any:
    from app.services import offers_service

    required = await offers_service.response.portal_otp_required(
        principal.tenant_id, principal.candidate_id, offer_id
    )
    return OfferOtpStatusOut(required=required)


@offers_router.post("/{offer_id}/otp", response_model=OtpSentOut)
async def offer_otp(offer_id: uuid.UUID, request: Request, principal: WorkerDep) -> Any:
    from app.services import offers_service

    expires, hint = await offers_service.response.portal_request_otp(
        principal.tenant_id,
        principal.candidate_id,
        offer_id,
        get_context().ip,
        request.headers.get("user-agent"),
    )
    return OtpSentOut(sent=True, expires_at=expires, email_hint=hint)
