"""Recruiter-side secure-link actions (revoke) and the Candidate 360 consent request (FR-67 via Epic 12)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import check
from app.models.pipeline import Screening, Submission
from app.schemas.pipeline import ConsentRequestIn, SecureLinkOut
from app.schemas.privacy import ConsentRequestResult
from app.services.pipeline.common import SCREEN, SUBMIT, application_for, principal_of

# Link purpose -> (subject model, permission needed to revoke).
_REVOCABLE: dict[str, tuple[type, str]] = {
    "ai_screening": (Screening, SCREEN),
    "client_feedback": (Submission, SUBMIT),
}


def _out(link: secure_links.SecureLink) -> SecureLinkOut:
    return SecureLinkOut(
        id=link.id,
        purpose=link.purpose,
        subject_type=link.subject_type,
        subject_id=link.subject_id,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        consumed_at=link.consumed_at,
    )


async def revoke_link(ctx: RequestContext, db: AsyncSession, link_id: uuid.UUID) -> SecureLinkOut:
    """Revoke (by a recruiter) a screening / feedback / consent link they can act on (else 404 / 403)."""
    principal = principal_of(ctx)
    link = await db.get(secure_links.SecureLink, link_id)
    if link is None:
        raise NotFound("Link not found")
    if link.purpose == "consent_request":
        check(principal, "consent:write")
    elif link.purpose in _REVOCABLE:
        model, permission = _REVOCABLE[link.purpose]
        subject = await db.get(model, link.subject_id)
        if subject is None:
            raise NotFound("Link not found")
        try:
            await application_for(ctx, db, subject.application_id, permission)
        except NotFound as exc:
            raise NotFound("Link not found") from exc
    else:
        raise NotFound("Link not found")
    if await secure_links.revoke(db, link.id):
        audit.record("secure_link.revoked", "secure_link", link.id, session=db, purpose=link.purpose)
    await db.refresh(link)
    return _out(link)


async def request_consent(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: ConsentRequestIn
) -> ConsentRequestResult:
    """Candidate 360 "Send consent request" (e.g. ``client_submission`` for one client) via Epic 12."""
    from app.services import privacy
    from app.services.candidates import pipeline_port as candidates

    if not await candidates.exists_visible(ctx, db, candidate_id):
        raise NotFound("Candidate not found")
    if data.client_id is not None:
        from app.services import clients_service

        if await clients_service.client_name(db, data.client_id) is None:
            raise NotFound("Client not found")
    return await privacy.request_consents(ctx, db, [candidate_id], data.purpose, data.client_id)
