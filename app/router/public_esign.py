"""Public eSignature endpoints (Epic 22): the secure-link signing ceremony, the offer acceptance code and the
public certificate check. P1 public-link rules apply: no session / CSRF, ``Origin`` check on POST, per-IP and
per-token rate limits, ``no-store`` / ``noindex``; the token is masked in logs by the redactor."""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from app.core import rate_limit
from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.settings import get_settings
from app.router.public import NO_STORE, LinkToken, Token
from app.schemas.documents import (
    CeremonyActionOut,
    CeremonyOut,
    ConsentIn,
    DeclineSignIn,
    OtpSentOut,
    PublicVerifyOut,
    SignIn,
)
from app.schemas.documents_portal import OfferOtpStatusOut
from app.services.documents import public as esign_public
from app.services.documents import verify

router = APIRouter(prefix="/public", tags=["public-esign"])


async def _esign_action(request: Request, token: Token) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin != get_settings().frontend_url.rstrip("/"):
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")
    await rate_limit.enforce("esign_action", hashlib.sha256(token.encode()).hexdigest()[:32], 30, 3600)


Action = Annotated[None, Depends(_esign_action)]


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.get("/links/{token}/esign", response_model=CeremonyOut)
async def esign_ceremony(token: LinkToken) -> Any:
    return await esign_public.ceremony(token)


@router.get("/links/{token}/esign/view", response_class=Response)
async def esign_view(token: LinkToken, request: Request) -> Response:
    """The frozen PDF (records a ``viewed`` event only; mail-scanner GETs never change a status)."""
    data, name = await esign_public.view(token, get_context().ip, _ua(request))
    safe = "".join(c for c in name if c.isalnum() or c in "._-") or "document.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            **NO_STORE,
            "Content-Disposition": f'inline; filename="{safe}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/links/{token}/esign/otp", response_model=OtpSentOut)
async def esign_otp(token: LinkToken, request: Request, _: Action) -> Any:
    expires, hint = await esign_public.request_otp(token, get_context().ip, _ua(request))
    return OtpSentOut(sent=True, expires_at=expires, email_hint=hint)


@router.post("/links/{token}/esign/consent", response_model=CeremonyActionOut)
async def esign_consent(token: LinkToken, body: ConsentIn, request: Request, _: Action) -> Any:
    await esign_public.consent(token, body.agreed, get_context().ip, _ua(request))
    return CeremonyActionOut(status="consented", request_status="open")


@router.post("/links/{token}/esign/sign", response_model=CeremonyActionOut)
async def esign_sign(token: LinkToken, body: SignIn, request: Request, _: Action) -> Any:
    status = await esign_public.sign(
        token, body.typed_name, body.otp, body.agreed, get_context().ip, _ua(request)
    )
    return CeremonyActionOut(status="signed", request_status=status)


@router.post("/links/{token}/esign/decline", response_model=CeremonyActionOut)
async def esign_decline(token: LinkToken, body: DeclineSignIn, request: Request, _: Action) -> Any:
    status = await esign_public.decline(token, body.reason, get_context().ip, _ua(request))
    return CeremonyActionOut(status="declined", request_status=status)


# ---------------------------------------------------------------- P1 offer links (purpose offer_response)
@router.get("/links/{token}/offer-otp", response_model=OfferOtpStatusOut)
async def offer_otp_status(token: LinkToken) -> Any:
    from app.services import offers_service

    return OfferOtpStatusOut(required=await offers_service.response.link_otp_required(token))


@router.post("/links/{token}/offer-otp", response_model=OtpSentOut)
async def offer_otp(token: LinkToken, request: Request, _: Action) -> Any:
    from app.services import offers_service

    expires, hint = await offers_service.response.link_request_otp(token, get_context().ip, _ua(request))
    return OtpSentOut(sent=True, expires_at=expires, email_hint=hint)


# ---------------------------------------------------------------- certificate check (/verify?c=)
@router.get("/verify/{certificate_id}", response_model=PublicVerifyOut)
async def public_verify(certificate_id: uuid.UUID, response: Response) -> Any:
    for key, value in NO_STORE.items():
        response.headers[key] = value
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("esign_verify_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 30, 60)
    return await verify.public_verify(certificate_id)
