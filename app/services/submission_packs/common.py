# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the ``submission_packs`` domain (Epic 61).

Access: a pack follows its Application. Reads need ``submission:read`` on the Application's Demand scope (else 404);
preparing needs ``submission:draft`` or ``submission:create``; human edits need ``submission:create``. A context without
a principal (system: hooks, the checks-only fallback) bypasses visibility, as the P1 pipeline helpers do.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound
from app.core.permissions import Principal, can_view_compensation, forbidden
from app.models.pipeline import Application, PipelineStage
from app.models.submission_packs import SubmissionPack

READ = "application:read"
PACK_READ = "submission:read"
DRAFT = "submission:draft"
SUBMIT = "submission:create"
CONFIGURE = "pipeline:configure"
CONSENT_PURPOSE = "client_submission"
AGENT_KEY = "submission"
SEQUENCE_KEY = "submission_pack"
SEQUENCE_PREFIX = "PCK"


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "submission_packs needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    return p if isinstance(p, Principal) else None


def is_system(ctx: RequestContext) -> bool:
    return principal(ctx) is None and getattr(ctx, "actor_kind", None) != "agent"


def can_comp(ctx: RequestContext) -> bool:
    """May this context see / store compensation amounts (system contexts may; reads are masked per viewer)."""
    p = principal(ctx)
    return True if p is None and is_system(ctx) else can_view_compensation(p)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


async def application_for(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    permission: str,
    *,
    missing_is_404: bool = True,
) -> Application:
    """The Application if the caller may see it and holds ``permission`` on its scope.

    Read permissions answer 404 when not held (the pack's existence is not disclosed); write permissions 403.
    """
    if principal(ctx) is None:
        app = await db.get(Application, application_id)
        if app is None:
            raise NotFound("Application not found")
        return app
    from app.services.pipeline.common import application_for as p1_application_for

    try:
        return await p1_application_for(ctx, db, application_id, permission)
    except Forbidden:
        if missing_is_404:
            raise NotFound("Application not found") from None
        raise


async def application_for_any(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, permissions: tuple[str, ...]
) -> Application:
    """``application_for`` with the first held permission of ``permissions`` (403 when none is in scope)."""
    p = principal(ctx)
    if p is None:
        return await application_for(ctx, db, application_id, READ)
    held = [perm for perm in permissions if perm in p.permissions]
    if not held:
        await application_for(ctx, db, application_id, READ)  # 404 first when invisible
        raise forbidden(permissions[0])
    last: Exception | None = None
    for perm in held:
        try:
            return await application_for(ctx, db, application_id, perm, missing_is_404=False)
        except Forbidden as exc:
            last = exc
    assert last is not None
    raise last


async def stage_key(db: AsyncSession, app: Application) -> str:
    stage = await db.get(PipelineStage, app.stage_id)
    return stage.key if stage is not None else ""


async def open_pack(
    db: AsyncSession, application_id: uuid.UUID, *, for_update: bool = False
) -> SubmissionPack | None:
    stmt = select(SubmissionPack).where(
        SubmissionPack.application_id == application_id, SubmissionPack.lifecycle == "open"
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def word_count(text: str | None) -> int:
    return len((text or "").split())


__all__ = [
    "AGENT_KEY",
    "CONFIGURE",
    "CONSENT_PURPOSE",
    "DRAFT",
    "PACK_READ",
    "READ",
    "SEQUENCE_KEY",
    "SEQUENCE_PREFIX",
    "SUBMIT",
    "application_for",
    "application_for_any",
    "can_comp",
    "conflict",
    "is_system",
    "now",
    "open_pack",
    "principal",
    "stage_key",
    "tenant_of",
    "word_count",
]
