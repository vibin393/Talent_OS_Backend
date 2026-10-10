# ruff: noqa: E501
"""Copy drafts (Epic 78 A4/A5, FR-403): projection, first-open marker (SM-C23), edits (checks re-run; an approved
draft becomes ``edited`` and loses its approval), warning acknowledgement and discard."""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.campaign_copy import CampaignCopyDraft
from app.schemas.campaign_copy import CopyDraftOut, DraftEditIn, Finding
from app.services.campaign_copy import checks
from app.services.campaign_copy import context as facts
from app.services.campaign_copy.common import (
    conflict,
    draft_hash,
    human_user,
    load_draft,
    load_request,
    now,
    require_perm,
)

EDITABLE = ("generated", "checked", "edited", "approved")


def is_blocked(d: CampaignCopyDraft) -> bool:
    return checks.blocked(d.findings or [])


def needs_ack(d: CampaignCopyDraft) -> bool:
    return checks.has_warnings(d.findings or []) and d.warnings_acknowledged_by is None


async def approver_names(db: AsyncSession, rows: Iterable[CampaignCopyDraft]) -> dict[uuid.UUID, str]:
    from app.services import entity_refs

    out: dict[uuid.UUID, str] = {}
    for uid in {d.approved_by for d in rows if d.approved_by}:
        assert uid is not None
        info = await entity_refs.user(db, uid)
        out[uid] = info.name if info is not None else ""
    return out


def to_out(d: CampaignCopyDraft, names: dict[uuid.UUID, str] | None = None) -> CopyDraftOut:
    return CopyDraftOut(
        id=d.id, request_id=d.request_id, segment_key=d.segment_key, segment_label=d.segment_label,
        variant_no=d.variant_no, channel=d.channel, subject=d.subject, preheader=d.preheader,  # type: ignore[arg-type]
        body_html=d.body_html, template_values={str(k): str(v) for k, v in (d.template_values or {}).items()} if d.template_values is not None else None,
        job_link_blocks=list(d.job_link_blocks or []), status=d.status,
        findings=[Finding(**f) for f in d.findings or []], blocked=is_blocked(d), needs_ack=needs_ack(d),
        warnings_acknowledged=d.warnings_acknowledged_by is not None, ai_generated=d.ai_generated,
        approved_by=d.approved_by, approved_by_name=(names or {}).get(d.approved_by) if d.approved_by else None,
        approved_at=d.approved_at, rubber_stamp=d.rubber_stamp, edited_at=d.edited_at,
        used_in_campaign_id=d.used_in_campaign_id, used_at=d.used_at, content_sha256=d.content_sha256,
    )  # fmt: skip


def content_of(d: CampaignCopyDraft) -> dict[str, Any]:
    return {
        "subject": d.subject, "preheader": d.preheader, "body_html": d.body_html,
        "template_values": dict(d.template_values or {}), "job_link_blocks": list(d.job_link_blocks or []),
    }  # fmt: skip


async def recheck(db: AsyncSession, d: CampaignCopyDraft, ctx_facts: checks.CheckContext | None = None) -> None:
    if ctx_facts is None:
        ctx_facts = await facts.check_context(db, await load_request(db, d.request_id))
    d.findings = checks.run(content_of(d), ctx_facts)
    d.content_sha256 = draft_hash(d)


async def _out(db: AsyncSession, d: CampaignCopyDraft) -> CopyDraftOut:
    return to_out(d, await approver_names(db, [d]))


async def mark_opened(ctx: RequestContext, db: AsyncSession, draft_id: uuid.UUID) -> None:
    principal = human_user(ctx)
    require_perm(principal, "campaign:copy_approve")
    d = await load_draft(db, draft_id, lock=True)
    if d.opened_at is None:
        d.opened_at = now()
        await db.flush()


async def mark_segment_opened(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, segment_key: str) -> int:
    principal = human_user(ctx)
    require_perm(principal, "campaign:copy_approve")
    rows = list(
        (
            await db.execute(
                select(CampaignCopyDraft).where(
                    CampaignCopyDraft.request_id == request_id,
                    CampaignCopyDraft.segment_key == segment_key,
                    CampaignCopyDraft.opened_at.is_(None),
                )
            )
        ).scalars()
    )
    for d in rows:
        d.opened_at = now()
    await db.flush()
    return len(rows)


async def edit_draft(ctx: RequestContext, db: AsyncSession, draft_id: uuid.UUID, data: DraftEditIn) -> CopyDraftOut:
    principal = human_user(ctx)
    d = await load_draft(db, draft_id, lock=True)
    require_perm(principal, "campaign:write" if d.campaign_id else "sequence:write")
    if d.status not in EDITABLE:
        raise conflict("draft_not_editable", f"A {d.status} draft cannot be edited", status=d.status)
    before = d.content_sha256
    fields = data.model_dump(exclude_unset=True)
    for key in ("subject", "preheader", "body_html", "template_values"):
        if key in fields:
            setattr(d, key, fields[key])
    if "job_link_blocks" in fields and data.job_link_blocks is not None:
        d.job_link_blocks = [b.model_dump(mode="json") for b in data.job_link_blocks]
    await recheck(db, d)
    if d.content_sha256 != before:
        was_approved = d.status == "approved"
        d.status = "edited"
        d.edited_by, d.edited_at = principal.id, now()
        d.warnings_acknowledged_by = None
        if was_approved:
            d.approved_by = d.approved_at = d.approval_ms = d.approved_sha256 = None  # type: ignore[assignment]
            d.rubber_stamp = False
        audit.record(
            "campaign_copy.edited", "campaign_copy_draft", d.id, session=db,
            before_sha256=before, after_sha256=d.content_sha256, cleared_approval=was_approved,
        )  # fmt: skip
    await db.flush()
    return await _out(db, d)


async def acknowledge_warnings(ctx: RequestContext, db: AsyncSession, draft_id: uuid.UUID) -> CopyDraftOut:
    principal = human_user(ctx)
    require_perm(principal, "campaign:copy_approve")
    d = await load_draft(db, draft_id, lock=True)
    if d.status not in ("checked", "edited", "approved"):
        raise conflict("draft_not_editable", f"A {d.status} draft cannot be acknowledged", status=d.status)
    d.warnings_acknowledged_by = principal.id
    await db.flush()
    return await _out(db, d)


async def discard(ctx: RequestContext, db: AsyncSession, draft_id: uuid.UUID) -> CopyDraftOut:
    principal = human_user(ctx)
    d = await load_draft(db, draft_id, lock=True)
    require_perm(principal, "campaign:write" if d.campaign_id else "sequence:write")
    if d.status in ("used", "discarded"):
        raise conflict("draft_not_editable", f"A {d.status} draft cannot be discarded", status=d.status)
    d.status = "discarded"
    await db.flush()
    audit.record("campaign_copy.discarded", "campaign_copy_draft", d.id, session=db)
    return await _out(db, d)


__all__ = [
    "acknowledge_warnings",
    "approver_names",
    "content_of",
    "discard",
    "edit_draft",
    "is_blocked",
    "mark_opened",
    "mark_segment_opened",
    "needs_ack",
    "recheck",
    "to_out",
]
