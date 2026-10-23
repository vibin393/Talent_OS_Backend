# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of ``campaign_copy`` (Epic 78): human-only principal checks, content hashing, loaders."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound
from app.core.permissions import Principal
from app.models.campaign_copy import CampaignCopyDraft, CampaignCopyRequest

AGENT_KEY = "campaign_copy"
FEATURE = "agent_campaign_copy"
DEFAULT_MONTHLY_TOKENS = 500_000
FALLBACK_MESSAGE = "The AI couldn't draft copy. Start from one of your templates."
FEATURE_OFF_MESSAGE = "AI campaign copy is off. An AI Administrator can turn it on."
BUDGET_MESSAGE = "This month's AI budget is used up. AI features that run in bulk are paused until the budget is raised or the month ends."
RUBBER_STAMP_MS = 10_000
PURGE_DAYS = 180


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details)


def human_user(ctx: RequestContext) -> Principal:
    """A signed-in internal user acting as themselves; every automated actor -> 403 ``human_only_action``."""
    require_human(actor_from_ctx(ctx))
    p = ctx.principal
    if not isinstance(p, Principal) or p.type != "user":
        raise Forbidden("Only a person can perform this action", code="human_only_action")
    return p


def require_perm(p: Principal, permission: str) -> None:
    if not p.has(permission):
        raise Forbidden(f"This needs the {permission} permission", code="forbidden")


def sha256_of(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def links_of(blocks: Any) -> list[list[str]]:
    out: list[list[str]] = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("posting_id"):
            out.append([str(b.get("posting_id")), str(b.get("cta") or b.get("label") or "")])
    return out


def draft_hash(d: CampaignCopyDraft) -> str:
    """The draft's own content hash (subject, preheader, body, template values, job links)."""
    return sha256_of(
        {
            "subject": d.subject or "",
            "preheader": d.preheader or "",
            "body_html": d.body_html or "",
            "template_values": dict(d.template_values or {}),
            "job_links": links_of(d.job_link_blocks),
        }
    )


def campaign_copy_hash(
    channel: str,
    *,
    subject: str | None,
    body_html: str | None,
    channel_template_id: uuid.UUID | str | None,
    job_links: Any,
    ab_subjects: list[str] | None = None,
) -> str:
    """The copy hash of a Campaign / Sequence step as P3 stores it (compared by the submit / activation guards)."""
    if channel == "email":
        return sha256_of(
            {
                "subject": subject or "",
                "body_html": body_html or "",
                "ab": sorted(ab_subjects or []),
                "job_links": links_of(job_links),
            }
        )
    return sha256_of({"template": str(channel_template_id or "")})


async def load_request(db: AsyncSession, request_id: uuid.UUID, *, lock: bool = False) -> CampaignCopyRequest:
    stmt = select(CampaignCopyRequest).where(CampaignCopyRequest.id == request_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Copy request not found")
    return row


async def load_draft(db: AsyncSession, draft_id: uuid.UUID, *, lock: bool = False) -> CampaignCopyDraft:
    stmt = select(CampaignCopyDraft).where(CampaignCopyDraft.id == draft_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Draft not found")
    return row


__all__ = [
    "AGENT_KEY",
    "BUDGET_MESSAGE",
    "DEFAULT_MONTHLY_TOKENS",
    "FALLBACK_MESSAGE",
    "FEATURE",
    "FEATURE_OFF_MESSAGE",
    "PURGE_DAYS",
    "RUBBER_STAMP_MS",
    "campaign_copy_hash",
    "conflict",
    "draft_hash",
    "human_user",
    "links_of",
    "load_draft",
    "load_request",
    "now",
    "require_perm",
    "sha256_of",
    "tenant_of",
]
