# ruff: noqa: E501
"""Shared helpers of the calendar sync domain: clock, tenant, settings row, system context, Tasks."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.calendar_sync import CalendarSyncSettings

VAULT_OWNER = "calendar_connection"  # owner id = user_connections.id (Epic 66)
CHANNEL_SECRET = "channel_token"
WINDOW_BEFORE = timedelta(days=1)
WINDOW_AFTER = timedelta(days=90)
GOOGLE_TTL = timedelta(days=7)
MICROSOFT_TTL = timedelta(minutes=10080 - 60)
RENEW_MARGIN = {"google": timedelta(hours=24), "microsoft": timedelta(hours=12)}
DEDUP_SECONDS = 30
SAFETY_POLL_AFTER = timedelta(hours=6)
MOVE_TOLERANCE = timedelta(minutes=1)
TASK_SOURCE = "agent"  # tasks.source CHECK has no calendar value; system tasks use the agent source (deviation noted)


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def set_clock(clock: Callable[[], datetime] | None) -> None:
    global _clock  # tests freeze the clock
    _clock = clock or _system_clock


def now() -> datetime:
    return _clock()


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


def window(at: datetime | None = None) -> tuple[datetime, datetime]:
    t = at or now()
    return t - WINDOW_BEFORE, t + WINDOW_AFTER


async def settings_row(db: AsyncSession, *, create: bool = False) -> CalendarSyncSettings:
    row = (await db.execute(select(CalendarSyncSettings))).scalar_one_or_none()
    if row is not None:
        return row
    row = CalendarSyncSettings(id=uuid.uuid4(), tenant_id=tenant_of(db))
    if create:
        db.add(row)
        await db.flush()
        await db.refresh(row)
    else:  # transient defaults (sync off) without writing
        row.enabled, row.move_policy, row.poll_minutes = False, "restore", 15
        row.restore_limit_per_hour, row.org_source_enabled = 3, False
    return row


async def open_task(
    db: AsyncSession, *, owner_id: uuid.UUID | None, title: str, notes: str, entity_id: uuid.UUID
) -> uuid.UUID | None:
    """One open Task per (calendar_change entity, title) -- deduplicated."""
    from app.models.tasks import Task
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    if owner_id is None:
        return None
    title = title[:200]
    existing = (
        await db.execute(
            select(Task.id).where(
                Task.entity_type == "calendar_change", Task.entity_id == entity_id, Task.status == "open", Task.title == title
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    data = TaskCreate.model_construct(
        title=title, notes=notes[:4000], owner_id=owner_id, entity_type="calendar_change", entity_id=entity_id,
        due_at=None, due_all_day=False, priority="high", reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            out = await tasks_service.create_system_task(system_ctx(tenant_of(db)), db, data, source=TASK_SOURCE)
        return out.id
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("calendar sync: could not create the task {!r}", title)
        return None


async def user_name(db: AsyncSession, user_id: uuid.UUID | None) -> str:
    from app.models.identity import User

    if user_id is None:
        return "someone"
    user = await db.get(User, user_id)
    return user.name if user is not None else "someone"
