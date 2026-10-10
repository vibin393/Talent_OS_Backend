# ruff: noqa: E501, D401
"""Shared helpers of the ``screening_agent`` domain (Epic 57): constants, the tenant settings row, loaders, errors."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal
from app.models.screening_agent import (
    OPEN_STATUSES,
    ScreeningAgentSettings,
    ScreeningConversation,
    ScreeningQuestionnaireVersion,
)

AGENT_KEY = "screening"
FEATURE = "agent_screening"
CONSENT_PURPOSE = "ai_agent_communication"
CONSENT_SOURCE = "conversation"
LINK_PURPOSE = "agent_conversation"
LINK_SUBJECT = "screening_conversation"
TRANSCRIPT_KIND = "screening_conversation"
DEFAULT_STOP_WORDS = ("stop", "talk to a person", "human", "speak to a human", "real person", "unsubscribe")
CONSENT_YES = ("yes", "y", "agree", "ok", "i agree", "okay")
CONSENT_NO = ("no", "n", "stop", "no thanks")
HANDOFF_ACK = "A recruiter will get back to you on that."
WA_WINDOW_HOURS = 24
PERMISSION = "screening:conduct"


def now() -> datetime:
    return datetime.now(UTC)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "screening_agent needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "user":
        return principal.id
    return None


async def settings_row(db: AsyncSession) -> ScreeningAgentSettings:
    """The tenant's settings row (created lazily with the defaults)."""
    row = (await db.execute(select(ScreeningAgentSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        pg_insert(ScreeningAgentSettings)
        .values(id=uuid.uuid4(), tenant_id=tenant_of(db), stop_words=list(DEFAULT_STOP_WORDS))
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return (await db.execute(select(ScreeningAgentSettings))).scalar_one()


def threshold(row: ScreeningAgentSettings) -> Decimal:
    return Decimal(str(row.low_confidence_threshold))


async def conversation(
    db: AsyncSession, conversation_id: uuid.UUID, *, for_update: bool = False
) -> ScreeningConversation:
    stmt = select(ScreeningConversation).where(ScreeningConversation.id == conversation_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Screening conversation not found")
    return row


async def version(db: AsyncSession, version_id: uuid.UUID) -> ScreeningQuestionnaireVersion:
    row = await db.get(ScreeningQuestionnaireVersion, version_id)
    if row is None:
        raise NotFound("Questionnaire version not found")
    return row


def is_open(conv: ScreeningConversation) -> bool:
    return conv.status in OPEN_STATUSES


def wa_window_open(conv: ScreeningConversation, at: datetime | None = None) -> bool:
    return conv.wa_window_until is not None and conv.wa_window_until > (at or now())


def required_ids(questions: list[dict[str, Any]]) -> list[uuid.UUID]:
    return [uuid.UUID(str(q["id"])) for q in questions if q.get("required")]


__all__ = [
    "AGENT_KEY",
    "CONSENT_PURPOSE",
    "CONSENT_SOURCE",
    "FEATURE",
    "LINK_PURPOSE",
    "PERMISSION",
    "conflict",
    "conversation",
    "invalid",
    "is_open",
    "now",
    "required_ids",
    "settings_row",
    "tenant_of",
    "threshold",
    "user_id_of",
    "version",
    "wa_window_open",
]
