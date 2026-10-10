"""Secure-link signing (Epic 22 A6): ``/public/links/{token}/esign/*`` for purpose ``esign`` (subject
``signer``). Every call resolves the link (404 / 410 per the P1 rules), opens the link's tenant session and
runs the shared ``SignerSession`` with channel ``secure_link``; signing consumes the single-use link."""

from datetime import datetime
from typing import Any

from app.core import secure_links
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.documents import SignatureRequest, Signer
from app.schemas.documents import CeremonyOut
from app.services.documents.common import ESIGN_LINK_PURPOSE, SIGNER_SUBJECT, tenant_name
from app.services.documents.esign import SignerSession


async def resolve(token: str) -> secure_links.ResolvedLink:
    link = await secure_links.resolve(token, (ESIGN_LINK_PURPOSE,))
    if link.subject_type != SIGNER_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    return link


async def link_summary(link: secure_links.ResolvedLink) -> tuple[str | None, str | None, str | None]:
    async with tenant_session(link.tenant_id) as db:
        signer = await db.get(Signer, link.subject_id)
        req = await db.get(SignatureRequest, signer.request_id) if signer else None
        return (req.status if req else None), (req.title if req else None), await tenant_name(db)


def _session(db: Any, link: secure_links.ResolvedLink, ip: str | None, ua: str | None) -> SignerSession:
    return SignerSession(db, link.subject_id, "secure_link", ip, ua, link_id=link.id)


async def ceremony(token: str) -> CeremonyOut:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        return await _session(db, link, None, None).ceremony(
            document_url=f"/api/v1/public/links/{token}/esign/view"
        )


async def view(token: str, ip: str | None, ua: str | None) -> tuple[bytes, str]:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        return await _session(db, link, ip, ua).view()


async def request_otp(token: str, ip: str | None, ua: str | None) -> tuple[datetime, str]:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        return await _session(db, link, ip, ua).request_otp()


async def consent(token: str, agreed: bool, ip: str | None, ua: str | None) -> None:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        await _session(db, link, ip, ua).consent(agreed)


async def sign(
    token: str, typed_name: str, otp: str | None, agreed: bool, ip: str | None, ua: str | None
) -> str:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        req = await _session(db, link, ip, ua).sign(typed_name, otp, agreed=agreed)
        return req.status


async def decline(token: str, reason: str | None, ip: str | None, ua: str | None) -> str:
    link = await resolve(token)
    async with tenant_session(link.tenant_id) as db:
        req = await _session(db, link, ip, ua).decline(reason)
        return req.status
