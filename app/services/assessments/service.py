"""Assessments contract (Epic 40; Epic 36 stub, AD-39). Signatures are FINAL
(``tests/contracts/signatures.json``). Epic 40 fills them (``invitations.py``)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.assessments import AssessmentInvitationSummary, AssessmentInviteIn

__all__ = ["get_invitation", "invite"]


async def invite(
    ctx: RequestContext, db: AsyncSession, req: AssessmentInviteIn
) -> AssessmentInvitationSummary:
    """Invite a candidate (user or workflow actor); records/requires the ``assessment`` notice; idempotent on
    ``req.idempotency_key``."""
    from app.services.assessments import invitations

    return await invitations.invite(ctx, db, req)


async def get_invitation(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID
) -> AssessmentInvitationSummary:
    from app.services.assessments import invitations

    return await invitations.get_invitation(ctx, db, invitation_id)
