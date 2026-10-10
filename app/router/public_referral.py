# ruff: noqa: E501
"""Public referee page (Epic 42, FR-214): ``/api/v1/public/links/{token}/referral`` (Secure Link purpose
``referral_link``). No session -- the token is the credential and is never logged; responses are ``no-store``,
``no-referrer``, ``noindex``; per-IP rate limit on every call (the share submit adds the bot challenge and a
per-link limit in the service).

* ``GET``: upload channel -> the ``referral`` notice with Accept / Decline; share channel -> the self-submit form.
* ``POST .../accept`` / ``.../decline``: one-shot (single-use link).
* ``POST`` (multipart): share channel -- submit details + resume and accept the notice in one step.
"""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, Response, UploadFile

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.referrals import PublicAcceptIn, PublicReferralOut, PublicReferralView
from app.services import files_service
from app.services.referrals import acceptance
from app.services.referrals.common import MAX_RESUME_MB

router = APIRouter(prefix="/public/links", tags=["public"])


async def _resume(file: UploadFile) -> tuple[bytes, str | None]:
    return await files_service.read_upload(file, MAX_RESUME_MB), file.filename


# ================================================================ public referee page (purpose referral_link)
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


async def _public_guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("referral_link_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], 60, 3600)
    return token


PublicToken = Annotated[str, Depends(_public_guard)]


@router.get("/{token}/referral", response_model=PublicReferralView)
async def public_view(token: PublicToken) -> Any:
    return await acceptance.view(token)


@router.post("/{token}/referral", response_model=PublicReferralOut)
async def public_share_submit(
    token: PublicToken,
    name: Annotated[str, Form(min_length=1, max_length=200)],
    email: Annotated[str, Form(min_length=3, max_length=320)],
    resume: Annotated[UploadFile, File()],
    agreed: Annotated[bool, Form()] = False,
    phone: Annotated[str | None, Form(max_length=40)] = None,
    notice_id: Annotated[uuid.UUID | None, Form()] = None,
    bot_token: Annotated[str | None, Form(max_length=4000)] = None,
) -> Any:
    data, filename = await _resume(resume)
    ctx = get_context()
    return await acceptance.share_submit(
        token,
        name=name,
        email=email,
        phone=phone or None,
        resume=data,
        filename=filename,
        notice_id=notice_id,
        agreed=agreed,
        bot_token=bot_token,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
    )


@router.post("/{token}/referral/accept", response_model=PublicReferralOut)
async def public_accept(token: PublicToken, body: PublicAcceptIn) -> Any:
    ctx = get_context()
    return await acceptance.accept(token, body.notice_id, ip=ctx.ip, user_agent=ctx.user_agent)


@router.post("/{token}/referral/decline", response_model=PublicReferralOut)
async def public_decline(token: PublicToken) -> Any:
    return await acceptance.decline(token)
