"""Public (unauthenticated) endpoints of Epic 12: sub-processor list and secure-link pages.

Secure-link responses carry ``Cache-Control: no-store``, ``Referrer-Policy: no-referrer`` and
``X-Robots-Tag: noindex``; the token path segment is masked by the log redactor. Rate limits: ``ip_anon``
(middleware) plus 20 requests per minute per token.
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Request, Response, status

from app.core import rate_limit
from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.settings import get_settings
from app.schemas.company_signup import SignupIn, SignupOut
from app.schemas.find_workspace import FindWorkspaceIn, FindWorkspaceOut
from app.schemas.interviews import PublicScorecardIn
from app.schemas.offers import AcceptIn, CandidateOfferView, DeclineIn, OfferActionOut
from app.schemas.pipeline import PublicActionOut, PublicFeedbackIn, PublicLinkView, PublicSubmitIn
from app.schemas.platform import SubProcessorOut
from app.schemas.privacy import PublicConsentIn, PublicConsentOut
from app.services import (
    company_onboarding,
    find_workspace_service,
    platform_service,
    privacy,
    public_links_service,
)

router = APIRouter(prefix="/public", tags=["public"])

Token = Annotated[str, Path(min_length=20, max_length=200)]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


async def _link_guard(response: Response, token: Token) -> str:
    for key, value in NO_STORE.items():
        response.headers[key] = value
    # Epic 6: 60 requests per minute per client IP on /public/links/*, then 20 per minute per token.
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("link_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 60, 60)
    await rate_limit.enforce("link_token", hashlib.sha256(token.encode()).hexdigest()[:32], 20, 60)
    return token


LinkToken = Annotated[str, Depends(_link_guard)]


async def _action_guard(request: Request, token: Token) -> None:
    """Epic 6 (NFR-17): one-shot POSTs need ``Origin == FRONTEND_URL`` (no session / CSRF on these routes)
    and are limited to 5 per token per hour."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin != get_settings().frontend_url.rstrip("/"):
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")
    await rate_limit.enforce("link_action", hashlib.sha256(token.encode()).hexdigest()[:32], 5, 3600)


ActionGuard = Annotated[None, Depends(_action_guard)]


@router.get("/sub-processors", response_model=list[SubProcessorOut])
async def public_sub_processors() -> list[SubProcessorOut]:
    return await platform_service.list_sub_processors(include_planned=True)


@router.post(
    "/find-workspace", response_model=FindWorkspaceOut, status_code=status.HTTP_202_ACCEPTED
)
async def find_workspace(body: FindWorkspaceIn, background: BackgroundTasks) -> FindWorkspaceOut:
    """Phase 2 Story 1.2: email the workspace link(s) for any tenant(s) this email belongs to.

    Enumeration-safe: always the same ``202 {status: "ok"}`` whether or not anything matched, and the email
    (sent only on a match) goes out as a background task so the response timing reveals nothing either.
    """
    # Rate limit per IP and per email (hashed): two refusals share the generic 429, which leaks no existence.
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("find_workspace_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 20, 3600)
    await rate_limit.enforce("find_workspace_email", rate_limit.subject_hash(body.email), 5, 3600)
    await find_workspace_service.find_workspaces(body.email, background)
    return FindWorkspaceOut()


@router.post("/signup", response_model=SignupOut)
async def signup(body: SignupIn) -> SignupOut:
    """Self-serve "Create your company": provision a new workspace across the unified platform and return
    the admin's invitation link.

    Unauthenticated (it is self-serve) and rate-limited per client IP. Local-first: there is no real email,
    so the invite link is RETURNED here (the success page shows it) instead of being emailed.
    """
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("signup_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 10, 3600)
    result = await company_onboarding.provision_company(
        slug=body.slug,
        company_name=body.company_name,
        admin_email=body.admin_email,
        admin_name=body.admin_name,
    )
    return SignupOut(
        tenant_slug=result.tenant_slug, admin_email=result.admin_email, invite_url=result.invite_url
    )


@router.get("/links/{token}", response_model=PublicLinkView)
async def view_link(token: LinkToken) -> PublicLinkView:
    # Epic 6: dispatch by purpose (consent_request -> privacy; ai_screening / client_feedback -> pipeline).
    return await public_links_service.view_link(token)


@router.post("/links/{token}/consent", response_model=PublicActionOut)
async def respond_consent(
    token: LinkToken, body: PublicConsentIn, request: Request, _: ActionGuard
) -> PublicConsentOut | PublicActionOut:
    ctx = get_context()
    ip_hash = hashlib.sha256((ctx.ip or "").encode()).hexdigest()[:32] if ctx.ip else None
    return await public_links_service.respond_consent(
        token, body.granted, body.notice_id, ip_hash=ip_hash, user_agent=request.headers.get("user-agent")
    )


@router.post("/links/{token}/submit", response_model=PublicActionOut)
async def submit_answers(token: LinkToken, body: PublicSubmitIn, _: ActionGuard) -> PublicActionOut:
    """Epic 6: AI screening answers (one-shot; consent must be given first)."""
    return await public_links_service.submit_answers(token, body.answers)


@router.post("/links/{token}/feedback", response_model=PublicActionOut)
async def client_feedback(token: LinkToken, body: PublicFeedbackIn, _: ActionGuard) -> PublicActionOut:
    """Epic 6: client feedback confirmation (one-shot; revokes the sibling links)."""
    return await public_links_service.client_feedback(token, body.decision, body.comment)


@router.post("/links/{token}/scorecard", response_model=PublicActionOut)
async def submit_scorecard(token: LinkToken, body: PublicScorecardIn, _: ActionGuard) -> PublicActionOut:
    """Epic 7: external interviewer scorecard (one-shot; before the interview starts -> 409)."""
    return await public_links_service.submit_scorecard(token, body)


@router.post("/links/{token}/opt-out", response_model=PublicActionOut)
async def channel_opt_out(token: LinkToken, _: ActionGuard) -> PublicActionOut:
    """Epic 33: stop SMS / WhatsApp messages (purpose ``channel_opt_out``; one of the link's 5 uses)."""
    return await public_links_service.channel_opt_out(token)


@router.get("/links/{token}/download", response_class=Response)
async def download_export(token: LinkToken) -> Response:
    content, filename = await privacy.export.download(token)
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "export.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ================================================================ Epic 8: offer response (purpose offer_response)
@router.get("/links/{token}/letter", response_class=Response)
async def offer_letter(token: LinkToken) -> Response:
    """Return the released offer letter (PDF, ``no-store``). Records nothing."""
    content, filename = await public_links_service.offer_letter(token)
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "offer.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={**NO_STORE, "Content-Disposition": f'inline; filename="{safe}"'},
    )


@router.post("/links/{token}/viewed", response_model=CandidateOfferView)
async def offer_viewed(token: LinkToken, request: Request, _: ActionGuard) -> CandidateOfferView:
    """Record the view; posted once by the page from JavaScript (mail-scanner GETs never record one)."""
    return await public_links_service.offer_viewed(token, get_context().ip, request.headers.get("user-agent"))  # type: ignore[no-any-return]


@router.post("/links/{token}/accept", response_model=OfferActionOut)
async def offer_accept(token: LinkToken, body: AcceptIn, request: Request, _: ActionGuard) -> OfferActionOut:
    return await public_links_service.offer_accept(  # type: ignore[no-any-return]
        token,
        typed_name=body.typed_name,
        version_no=body.version_no,
        agreed=body.agreed,
        ip=get_context().ip,
        user_agent=request.headers.get("user-agent"),
        otp=body.otp,
    )


@router.post("/links/{token}/decline", response_model=OfferActionOut)
async def offer_decline(
    token: LinkToken, body: DeclineIn, request: Request, _: ActionGuard
) -> OfferActionOut:
    return await public_links_service.offer_decline(  # type: ignore[no-any-return]
        token,
        reason=body.reason,
        version_no=body.version_no,
        ip=get_context().ip,
        user_agent=request.headers.get("user-agent"),
    )
