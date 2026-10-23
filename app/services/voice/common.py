# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the Voice Screening domain (Epic 58): constants, clock, settings row, loaders, consent states,
error helpers, system contexts, the session projection and the notification / timeline helpers."""

import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, NotFound, ValidationFailed
from app.core.permissions import Principal, Scope
from app.models.privacy import Consent
from app.models.voice import OPEN_STATUSES, VoiceSession, VoiceSettings
from app.schemas.voice import VoiceSessionOut

AGENT_KEY = "voice_screening"
FEATURE = "agent_voice_screening"
PURPOSE_CALL = "voice_screening"
PURPOSE_RECORDING = "call_recording"
CONSENT_SOURCE = "voice_consent_link"
LINK_PURPOSE = "voice_consent"
LINK_SUBJECT = "voice_session"
FILE_OWNER = "voice_session"
STATEMENT_VERSION = "2026-10-08"
TEMPLATE_KEY = "voice_consent_request"
UPLOAD = "voice:upload"
CONDUCT = "screening:conduct"


@dataclasses.dataclass(frozen=True, slots=True)
class FakeCallScheduled:
    """Internal (not catalogued) after-commit signal: play the Fake call lifecycle once the scheduling commits."""

    tenant_id: uuid.UUID
    voice_session_id: uuid.UUID


ACTIVE_FOR_WITHDRAWAL = (*OPEN_STATUSES, "ready_for_review")

_clock: Callable[[], datetime] | None = None


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now(UTC)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    global _clock
    _clock = fn


class TooLarge(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - mirrors HTTP 413 like the core errors
    status_code = 413
    code = "file_too_large"
    default_message = "The file is too large"


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def principal_of(ctx: RequestContext) -> Principal | None:
    return ctx.principal if isinstance(ctx.principal, Principal) else None


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = principal_of(ctx)
    if p is None:
        return None
    if p.type == "agent":
        return ctx.on_behalf_of
    return p.id if p.type == "user" else None


def reader_ctx(
    tenant_id: uuid.UUID, user_id: uuid.UUID, base: RequestContext | None = None
) -> RequestContext:
    """A context for the transcripts / gate Candidate visibility checks once the caller's access to the session
    (Application read) has been established, or for system work (consent page, tasks). The actor is unchanged."""
    principal = Principal(
        type="user", id=user_id, tenant_id=tenant_id,
        permissions={"candidate:read": Scope.TENANT, "application:read": Scope.TENANT},
    )  # fmt: skip
    if base is None:
        return RequestContext(tenant_id=tenant_id, principal=principal, actor_kind="system")
    return dataclasses.replace(base, principal=principal)


async def settings_row(db: AsyncSession) -> VoiceSettings:
    from app.services import entity_refs

    tenant_id = entity_refs.session_tenant(db)
    row = await db.get(VoiceSettings, tenant_id)
    if row is None:
        row = VoiceSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def session_row(db: AsyncSession, session_id: uuid.UUID, *, for_update: bool = False) -> VoiceSession:
    stmt = select(VoiceSession).where(VoiceSession.id == session_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Voice session not found")
    return row


async def consent_state(db: AsyncSession, row: VoiceSession, purpose: str) -> str:
    """``granted`` (active consent), ``refused`` (withdrawn since the session was requested) or ``pending``."""
    latest = (
        (
            await db.execute(
                select(Consent)
                .where(Consent.candidate_id == row.candidate_id, Consent.purpose == purpose)
                .order_by(Consent.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if any(c.status == "granted" for c in latest):
        return "granted"
    if any(c.status == "withdrawn" and (c.withdrawn_at or c.created_at) >= row.created_at for c in latest):
        return "refused"
    return "pending"


async def has_consent(db: AsyncSession, candidate_id: uuid.UUID, purpose: str) -> bool:
    from app.services.privacy.consent import has_active_consent

    return await has_active_consent(db, candidate_id, purpose)


async def active_consent_id(db: AsyncSession, candidate_id: uuid.UUID, purpose: str) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Consent.id)
            .where(
                Consent.candidate_id == candidate_id, Consent.purpose == purpose, Consent.status == "granted"
            )
            .order_by(Consent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def needs_speaker_label(db: AsyncSession, row: VoiceSession) -> bool:
    from app.models.transcripts import TranscriptSegment

    if row.transcript_id is None:
        return False
    roles = set(
        (
            await db.execute(
                select(TranscriptSegment.speaker_role).where(
                    TranscriptSegment.transcript_id == row.transcript_id
                )
            )
        ).scalars()
    )
    return bool(roles) and "candidate" not in roles


def block_reason(row: VoiceSession, recording_state: str) -> str | None:
    """Why a recording cannot be uploaded right now (UI hint; the gate is re-run on upload)."""
    if row.recording_rule in ("not_permitted", "unknown"):
        return "not_permitted"
    if recording_state != "granted":
        return "missing_consent"
    return None


async def out(db: AsyncSession, row: VoiceSession) -> VoiceSessionOut:
    await db.flush()
    await db.refresh(row)
    voice = await consent_state(db, row, PURPOSE_CALL)
    rec = await consent_state(db, row, PURPOSE_RECORDING)
    return VoiceSessionOut(
        id=row.id, human_id=row.human_id, application_id=row.application_id, candidate_id=row.candidate_id,
        mode=row.mode, provider_key=row.provider_key, status=row.status, external_ref=row.external_ref,  # type: ignore[arg-type]
        voice_consent=voice, recording_consent=rec, jurisdiction=row.jurisdiction, recording_rule=row.recording_rule,  # type: ignore[arg-type]
        recording_allowed=row.recording_allowed, recording_block_reason=block_reason(row, rec),
        has_recording=row.recording_file_id is not None, recording_media_type=row.recording_media_type,
        recording_deleted_at=row.recording_deleted_at, has_transcript=row.transcript_id is not None,
        transcript_id=row.transcript_id, transcript_source=row.transcript_source,
        needs_speaker_label=await needs_speaker_label(db, row), questionnaire_id=row.questionnaire_id,
        questionnaire_version=row.questionnaire_version, summary=row.summary, gaps=list(row.gaps or []),
        failure_code=row.failure_code, scheduled_at=row.scheduled_at, started_at=row.started_at, ended_at=row.ended_at,
        duration_s=row.duration_s, requested_by=row.requested_by, owner_user_id=row.owner_user_id,
        reviewed_by=row.reviewed_by, reviewed_at=row.reviewed_at, screening_id=row.screening_id,
        withdrawn_at=row.withdrawn_at, created_at=row.created_at, updated_at=row.updated_at,
    )  # fmt: skip


def fan_out(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> None:
    from app.tasks import notifications as notification_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)
    except Exception:
        logger.exception("Voice screening notification {} failed", type_key)


async def timeline(ctx: RequestContext, db: AsyncSession, row: VoiceSession, body: str, event: str) -> None:
    """A labels-only entry on the Application timeline (never transcript text)."""
    from app.services import activity_service

    if row.application_id is None:
        return
    try:
        async with db.begin_nested():
            await activity_service.add_system_entry(
                ctx, db, "application", row.application_id, body,
                {"kind": "voice_session", "event": event, "voice_session_id": str(row.id), "human_id": row.human_id},
            )  # fmt: skip
    except Exception:
        logger.exception("Voice session timeline entry failed")


async def owner_task(
    db: AsyncSession, row: VoiceSession, title: str, notes: str | None = None
) -> uuid.UUID | None:
    """A Task for the session owner on the Application (``source="agent"`` system task)."""
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    data = TaskCreate.model_construct(
        title=title[:200], notes=notes, owner_id=row.owner_user_id,
        entity_type="application" if row.application_id else "candidate",
        entity_id=row.application_id or row.candidate_id, due_at=None, due_all_day=False, priority="normal",
        reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            task = await tasks_service.create_system_task(
                RequestContext(tenant_id=row.tenant_id), db, data, source="agent"
            )
        return task.id
    except Exception:
        logger.exception("Voice session task failed")
        return None


async def candidate_name(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    from app.services import entity_refs

    return await entity_refs.entity_label(db, "candidate", candidate_id) or "the candidate"


__all__ = [
    "AGENT_KEY",
    "CONDUCT",
    "CONSENT_SOURCE",
    "FEATURE",
    "FILE_OWNER",
    "LINK_PURPOSE",
    "LINK_SUBJECT",
    "PURPOSE_CALL",
    "PURPOSE_RECORDING",
    "STATEMENT_VERSION",
    "TEMPLATE_KEY",
    "UPLOAD",
    "TooLarge",
    "active_consent_id",
    "candidate_name",
    "conflict",
    "consent_state",
    "fan_out",
    "has_consent",
    "invalid",
    "now",
    "out",
    "owner_task",
    "reader_ctx",
    "session_row",
    "settings_row",
    "timeline",
    "user_id_of",
]
