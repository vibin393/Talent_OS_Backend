# ruff: noqa: E501
"""Shared helpers of the ``assessments`` domain (Epic 40): permission keys, errors, clock, tenant / actor access."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed

ASM_READ = "assessment:read"
ASM_AUTHOR = "assessment:author"
ASM_INVITE = "assessment:invite"
ASM_REVIEW = "assessment:review"
ASM_CONFIGURE = "assessment:configure"
LINK_PURPOSE = "assessment_attempt"
LINK_SUBJECT = "assessment_invitation"
CONSENT_PURPOSE = "assessment"
SUBMIT_GRACE_SECONDS = 30
MAX_ANSWER_BYTES = 20 * 1024


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, field: str | None = None, **details: Any) -> ValidationFailed:
    extra: dict[str, Any] = dict(details)
    if field:
        extra["fields"] = [{"field": field, "message": message}]
    return ValidationFailed(message, code=code, details=extra or None)


def not_found(what: str = "Assessment") -> NotFound:
    return NotFound(f"{what} not found")


def actor_of(ctx: RequestContext) -> ActorRef:
    return actor_from_ctx(ctx)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None


__all__ = [
    "ASM_AUTHOR",
    "ASM_CONFIGURE",
    "ASM_INVITE",
    "ASM_READ",
    "ASM_REVIEW",
    "CONSENT_PURPOSE",
    "LINK_PURPOSE",
    "LINK_SUBJECT",
    "MAX_ANSWER_BYTES",
    "SUBMIT_GRACE_SECONDS",
    "actor_of",
    "conflict",
    "invalid",
    "not_found",
    "now",
    "tenant_of",
    "user_id",
]
