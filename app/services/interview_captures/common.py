# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the Interview Assistant domain (Epic 60): clock, settings row, loaders, the kit view and the
participants split (internal Users vs external interviewers)."""

import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal
from app.models.interview_captures import InterviewAssistantSettings, InterviewCapture
from app.models.interviews import Interview, InterviewParticipant

FEATURE = "agent_interview_assist"  # coordinator decision C-17
AGENT_KEY = "interview_assist"
PURPOSE = "interview_recording"
CONSENT_SOURCE = "interview_invite"
STATEMENT_VERSION = "2026-10-08"
CONSENT_LINK_PURPOSE = "interview_recording_consent"
ACK_LINK_PURPOSE = "interview_capture_ack"
CAPTURE_SUBJECT = "interview_capture"
PARTICIPANT_SUBJECT = "interview_participant"
CAPTURABLE_MODES = ("video", "phone", "in_person")
CAPTURABLE_STATUSES = ("scheduled", "completed")
OPEN_STATUSES = ("planned", "consent_pending", "blocked")
MANAGE = "interview_capture:manage"
DEFAULT_COMPETENCIES = [{"key": "overall", "label": "Overall", "guidance": None}]

_clock: Callable[[], datetime] | None = None


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now(UTC)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    global _clock
    _clock = fn


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.tenant_id is None:
        raise NotFound("Interview not found")
    return principal


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type in ("user", "agent"):
        return ctx.on_behalf_of or principal.id if principal.type == "agent" else principal.id
    return None


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


async def settings_row(db: AsyncSession) -> InterviewAssistantSettings:
    from app.services import entity_refs

    tenant_id = entity_refs.session_tenant(db)
    row = await db.get(InterviewAssistantSettings, tenant_id)
    if row is None:
        row = InterviewAssistantSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def interview_row(db: AsyncSession, interview_id: uuid.UUID, *, for_update: bool = False) -> Interview:
    stmt = select(Interview).where(Interview.id == interview_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Interview not found")
    return row


async def capture_for_interview(
    db: AsyncSession, interview_id: uuid.UUID, *, for_update: bool = False
) -> InterviewCapture | None:
    stmt = select(InterviewCapture).where(InterviewCapture.interview_id == interview_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def capture_row(
    db: AsyncSession, capture_id: uuid.UUID, *, for_update: bool = False
) -> InterviewCapture:
    stmt = select(InterviewCapture).where(InterviewCapture.id == capture_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Interview capture not found")
    return row


async def participants(db: AsyncSession, interview_id: uuid.UUID) -> list[InterviewParticipant]:
    stmt = (
        select(InterviewParticipant)
        .where(InterviewParticipant.interview_id == interview_id, InterviewParticipant.removed_at.is_(None))
        .order_by(InterviewParticipant.created_at, InterviewParticipant.id)
    )
    return list((await db.execute(stmt)).scalars())


def interviewers(
    parts: list[InterviewParticipant],
) -> tuple[list[InterviewParticipant], list[InterviewParticipant]]:
    """``(internal, external)``: every non-candidate participant must agree (FR-301). Internal = has a User."""
    people = [p for p in parts if p.kind != "candidate"]
    return [p for p in people if p.user_id is not None], [p for p in people if p.user_id is None]


@dataclasses.dataclass(frozen=True)
class KitView:
    competencies: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    rating_scale: int

    @property
    def competency_keys(self) -> list[str]:
        return [str(c["key"]) for c in self.competencies]

    @property
    def question_keys(self) -> list[str]:
        return [str(q["key"]) for q in self.questions]


def kit_of(row: Interview) -> KitView:
    kit = dict(row.kit_snapshot or {})
    competencies = list(kit.get("competencies") or []) or list(DEFAULT_COMPETENCIES)
    return KitView(
        competencies=[
            {"key": str(c.get("key")), "label": str(c.get("label") or c.get("key")), "guidance": c.get("guidance")}
            for c in competencies
        ],
        questions=[
            {
                "key": str(q.get("key")), "competency_key": q.get("competency_key"), "text": str(q.get("text") or ""),
                "guidance": q.get("guidance"),
            }
            for q in kit.get("questions") or []
        ],
        rating_scale=int(kit.get("rating_scale") or 5),
    )  # fmt: skip


__all__ = [
    "ACK_LINK_PURPOSE",
    "AGENT_KEY",
    "CAPTURE_SUBJECT",
    "CONSENT_LINK_PURPOSE",
    "CONSENT_SOURCE",
    "FEATURE",
    "MANAGE",
    "PURPOSE",
    "STATEMENT_VERSION",
    "KitView",
    "capture_for_interview",
    "capture_row",
    "conflict",
    "interview_row",
    "interviewers",
    "invalid",
    "kit_of",
    "now",
    "participants",
    "settings_row",
]
