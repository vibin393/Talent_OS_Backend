"""Shared plumbing of the interviews service: clock, principal helpers, permissions, errors, settings row."""

import contextlib
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.permissions import Principal
from app.models.interviews import InterviewSettings

READ = "interview:read"
SCHEDULE = "interview:schedule"
CONFIGURE = "interview:configure"
OUTCOME = "interview:outcome"
SCORECARD_SUBMIT = "scorecard:submit"

DEFAULT_TEMPLATE_NAME = "Technical → Client → HR"
DEFAULT_TEMPLATE_STAGES: list[dict[str, Any]] = [
    {"name": "Technical", "kind": "technical", "kit_id": None, "default_duration_minutes": 60},
    {"name": "Client", "kind": "client", "kit_id": None, "default_duration_minutes": 60},
    {"name": "HR", "kind": "hr", "kit_id": None, "default_duration_minutes": 30},
]
RECOMMENDATION_OPTIONS = ["strong_yes", "yes", "no", "strong_no"]


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """Replace the interviews clock in tests (reminders, completion, scorecard timing); None restores it."""
    global _clock  # process-wide test hook
    _clock = clock or _system_clock


def now() -> datetime:
    return _clock()


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return principal.id if isinstance(principal, Principal) and principal.type == "user" else None


def actor_kind_of(ctx: RequestContext) -> str:
    """``user`` for a signed-in user; a workflow / ai / agent context reports its kind; else system / api_key."""
    if getattr(ctx, "actor_kind", None) in ("workflow", "ai", "agent", "app"):
        return str(ctx.actor_kind)
    kind = getattr(ctx.principal, "type", None)
    if kind in ("user", "api_key", "app"):  # Epic 66: +app
        return str(kind)
    return "system"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "interviews service needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


@contextlib.contextmanager
def acting_as(db: AsyncSession, actor_type: str, actor: uuid.UUID | None = None) -> Iterator[None]:
    info = db.sync_session.info
    previous = info.get(audit.ACTOR_INFO_KEY)
    audit.set_actor(db, actor_type, actor)
    try:
        yield
    finally:
        if previous is None:
            info.pop(audit.ACTOR_INFO_KEY, None)
        else:
            info[audit.ACTOR_INFO_KEY] = previous


async def settings_row(db: AsyncSession) -> InterviewSettings:
    row = (await db.execute(select(InterviewSettings))).scalar_one_or_none()
    if row is None:
        row = InterviewSettings(tenant_id=tenant_of(db), reminder_offsets_minutes=[1440, 60])
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def offsets(row: InterviewSettings) -> list[int]:
    return sorted({int(x) for x in (row.reminder_offsets_minutes or []) if int(x) > 0}, reverse=True)
