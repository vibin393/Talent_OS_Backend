# ruff: file-ignore[line-too-long]
"""Consent and notices of the portal Copilot (Epic 77 A6).

* Candidates: the P4 ``ai_agent_communication`` consent is required before the first question; the panel shows the
  tenant's active notice for that purpose and ``grant`` records the consent (source ``candidate_portal``; the P5
  ``portal`` source is not in the P1 consent-source CHECK -- Implementation Notes).
* Client / Vendor Users: the ``portal_copilot`` notice (seed text owned here). Consent rows are Candidate rows in P1, so
  a Client / Vendor User's first use is recorded as an audit acknowledgement (``portal_copilot.notice_acknowledged``)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.services.portal_copilot.principals import CANDIDATE_CONSENT_PURPOSE, PortalActor

NOTICE_PURPOSE = "portal_copilot"
NOTICE_TITLE = "Portal assistant"
NOTICE_BODY = (
    "The portal assistant uses AI to answer questions about your records with {tenant}. It can only see what you can "
    "see in this portal. Conversations are kept for {n} days."
)


def register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice(NOTICE_PURPOSE, NOTICE_TITLE, NOTICE_BODY)


async def candidate_notice(idb: AsyncSession, tenant_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    from app.services.privacy.consent import active_notice

    notice = await active_notice(idb, tenant_id, CANDIDATE_CONSENT_PURPOSE)
    return (notice.id, notice.body_md) if notice is not None else None


async def grant(ctx: RequestContext, idb: AsyncSession, actor: PortalActor, notice_id: uuid.UUID) -> None:
    from app.services import privacy
    from app.services.privacy.consent import active_notice

    notice = await active_notice(idb, actor.tenant_id, CANDIDATE_CONSENT_PURPOSE)
    if notice is None or notice.id != notice_id:
        raise ConflictError(
            "The privacy notice changed; please review it again", code="notice_outdated",
            details={"notice_id": str(notice.id) if notice else None},
        )  # fmt: skip
    cctx = RequestContext(
        request_id=ctx.request_id,
        tenant_id=actor.tenant_id,
        principal=actor.core_principal(),
        ip=ctx.ip,
        user_agent=ctx.user_agent,
    )
    await privacy.grant_consent(
        cctx, idb, actor.principal_id, CANDIDATE_CONSENT_PURPOSE, source="candidate_portal", notice_id=notice.id,
        evidence={"channel": "portal_copilot", "session_id": str(actor.session_id) if actor.session_id else None},
    )  # fmt: skip


def acknowledge(idb: AsyncSession, actor: PortalActor) -> None:
    audit.record(
        "portal_copilot.notice_acknowledged", "portal_copilot_notice", actor.principal_id, session=idb,
        principal_type=actor.principal_type,
    )  # fmt: skip


__all__ = [
    "NOTICE_BODY",
    "NOTICE_PURPOSE",
    "NOTICE_TITLE",
    "acknowledge",
    "candidate_notice",
    "grant",
    "register_notice",
]
