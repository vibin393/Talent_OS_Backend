"""Shared constants and helpers of the ``knowledge`` domain (Epic 62)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.permissions import Principal
from app.services import entity_refs

CONSENT_PURPOSE = "knowledge_assistant"
AGENT_KEY = "onboarding"
HANDOFF_TOPICS = ("pay", "tax", "legal", "immigration")
QUESTIONS_PER_HOUR = 20
MAX_ASKER_TEXT = 2000
MAX_ANSWER_TEXT = 1500
HANDOFF_TEXT = "I'll pass this to your coordinator."
UNAVAILABLE_TEXT = "The onboarding assistant is not available. Your question was sent to your coordinator."
EXCERPT_CHARS = 600
WAIT_HOURS = 72
# Shown read-only in the UI (tenant overrides are a Phase 4 known gap).
CADENCE = {
    "handoff_topics": list(HANDOFF_TOPICS),
    "questions_per_hour": QUESTIONS_PER_HOUR,
    "wait_hours": WAIT_HOURS,
}


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    return ctx.tenant_id or entity_refs.session_tenant(db)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


def not_found(what: str = "Resource") -> NotFound:
    return NotFound(f"{what} not found")


def conflict(code: str, message: str, **details: object) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


__all__ = [
    "AGENT_KEY",
    "CADENCE",
    "CONSENT_PURPOSE",
    "EXCERPT_CHARS",
    "HANDOFF_TEXT",
    "HANDOFF_TOPICS",
    "MAX_ANSWER_TEXT",
    "MAX_ASKER_TEXT",
    "QUESTIONS_PER_HOUR",
    "UNAVAILABLE_TEXT",
    "WAIT_HOURS",
    "conflict",
    "not_found",
    "now",
    "tenant_of",
    "user_id",
]
