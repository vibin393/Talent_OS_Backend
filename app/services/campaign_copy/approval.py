# ruff: noqa: E501
"""★ Approval of AI-drafted copy (Epic 78 A5, FR-403, FR-409). Human only: every automated actor (``agent``, ``app``,
``api_key``, ``ai``, ``workflow``, ``system``) -> 403 ``human_only_action``. Needs ``campaign:copy_approve``, no
``block`` finding and, when there are warnings, an acknowledgement. SM-C23: an approval of an unopened draft or within
10 s of opening it is flagged ``rubber_stamp``. Approving one variant never approves its siblings."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.events.campaign_copy import CampaignCopyApproved
from app.schemas.campaign_copy import ApproveIn, CopyDraftOut
from app.services.campaign_copy import drafts
from app.services.campaign_copy.common import (
    RUBBER_STAMP_MS,
    conflict,
    human_user,
    load_draft,
    now,
    require_perm,
)


async def approve(ctx: RequestContext, db: AsyncSession, draft_id: uuid.UUID, data: ApproveIn) -> CopyDraftOut:
    principal = human_user(ctx)
    require_perm(principal, "campaign:copy_approve")
    d = await load_draft(db, draft_id, lock=True)
    if d.status not in ("checked", "edited"):
        raise conflict("draft_not_approvable", f"A {d.status} draft cannot be approved", status=d.status)
    await drafts.recheck(db, d)
    if drafts.is_blocked(d):
        raise conflict(
            "copy_checks_failed", "Fix the issues marked Must fix before approving",
            findings=[f for f in d.findings or [] if f.get("severity") == "block"],
        )  # fmt: skip
    if drafts.needs_ack(d):
        raise conflict("copy_warnings_unacknowledged", "Acknowledge the checks first")
    at = now()
    d.status = "approved"
    d.approved_by, d.approved_at = principal.id, at
    d.approved_sha256 = d.content_sha256
    d.approval_ms = int((at - d.opened_at).total_seconds() * 1000) if d.opened_at is not None else None
    d.rubber_stamp = d.opened_at is None or (d.approval_ms is not None and d.approval_ms < RUBBER_STAMP_MS)
    await db.flush()
    audit.record(
        "campaign_copy.approved", "campaign_copy_draft", d.id, session=db,
        after_sha256=d.content_sha256, rubber_stamp=d.rubber_stamp, approval_ms=d.approval_ms,
        note=(data.note or "")[:500],
    )  # fmt: skip
    emit(db, CampaignCopyApproved(
        tenant_id=d.tenant_id, draft_id=d.id, campaign_id=d.campaign_id, approved_by=principal.id, rubber_stamp=d.rubber_stamp,
    ))  # fmt: skip
    return await drafts._out(db, d)


__all__ = ["approve"]
