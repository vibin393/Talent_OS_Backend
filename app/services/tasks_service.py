"""Tasks service (Epic 10, story 10.2, FR-56).

* Scope: the ``task:*`` permissions with ``own`` = owner **or** creator (team / bu / tenant widen as usual).
* A linked entity must be readable by the creator (404), the owner an active user (422 ``invalid_owner``),
  ``reminder_at`` in the future and not after the due time (422 ``invalid_reminder``; all-day tasks: the end
  of the due day).
* Overdue = open and due in the past; an all-day task is overdue once its due date has ended in the owner's
  timezone (users carry no timezone yet: the tenant's). Responses carry ``is_overdue``.
* Events: ``TaskAssigned`` (owner != actor), ``TaskCompleted``, ``TaskReminderDue`` (``send_reminders``,
  run every minute per tenant, ``FOR UPDATE SKIP LOCKED``, once per task).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import acting_user_id
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, check, forbidden, scope_condition
from app.events.tasks import TaskAssigned, TaskCompleted, TaskReminderDue
from app.models.tasks import TASK_ENTITY_TYPES, Task
from app.schemas.tasks import TaskCreate, TaskFilters, TaskOut, TaskSummaryOut, TaskUpdate
from app.services import entity_refs

READ, CREATE, UPDATE, DELETE = "task:read", "task:create", "task:update", "task:delete"
_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
REMINDER_BATCH = 500


def _now() -> datetime:
    return datetime.now(UTC)


# Epic 67: the Public API calls these with App Principals and API keys (owner / creator = the acting user or NULL).
CALLER_TYPES = frozenset({"user", "app", "api_key"})


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in CALLER_TYPES or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def _actor_user(ctx: RequestContext, principal: Principal) -> uuid.UUID | None:
    return None if ctx.actor_kind == "workflow" else acting_user_id(principal)


# ================================================================ visibility
def _cond(principal: Principal, permission: str) -> ColumnElement[bool] | None:
    by_owner = scope_condition(principal, permission, Task.owner_id)
    by_creator = scope_condition(principal, permission, Task.created_by)
    if by_owner is None or by_creator is None:
        return None
    return or_(by_owner, by_creator)


def _visible(principal: Principal) -> Any:
    stmt = select(Task).where(Task.archived_at.is_(None))
    cond = _cond(principal, READ)
    return stmt if cond is None else stmt.where(cond)


async def _get(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, permission: str = READ) -> Task:
    principal = _principal(ctx)
    stmt = _visible(principal).where(Task.id == task_id)
    if permission != READ:
        stmt = stmt.with_for_update()
    task = (await db.execute(stmt)).scalar_one_or_none()
    if task is None:
        raise NotFound("Task not found")
    if permission != READ:
        check(principal, permission)
        cond = _cond(principal, permission)
        if (
            cond is not None
            and (await db.execute(select(Task.id).where(Task.id == task.id, cond))).first() is None
        ):
            raise forbidden(permission)
    return task


# ================================================================ time
async def _tz(db: AsyncSession) -> ZoneInfo:
    return (await entity_refs.tenant(db, entity_refs.session_tenant(db))).tz


def _end_of_local_day(value: datetime, tz: ZoneInfo) -> datetime:
    local_day = value.astimezone(tz).date()
    return datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)


def is_overdue(task: Task, tz: ZoneInfo, now: datetime | None = None) -> bool:
    if task.status != "open" or task.due_at is None:
        return False
    now = now or _now()
    if task.due_all_day:
        return now >= _end_of_local_day(task.due_at, tz)
    return task.due_at < now


def _local_date(tz: ZoneInfo) -> ColumnElement[Any]:
    return func.date(func.timezone(str(tz.key), Task.due_at))


def _overdue_cond(tz: ZoneInfo, now: datetime) -> ColumnElement[bool]:
    today = now.astimezone(tz).date()
    return and_(
        Task.status == "open",
        Task.due_at.is_not(None),
        or_(
            and_(Task.due_all_day.is_(False), Task.due_at < now),
            and_(Task.due_all_day.is_(True), _local_date(tz) < today),
        ),
    )


def _today(tz: ZoneInfo, now: datetime) -> date:
    return now.astimezone(tz).date()


# ================================================================ output
async def _outs(db: AsyncSession, tasks: Sequence[Task]) -> list[TaskOut]:
    tz = await _tz(db)
    now = _now()
    names = await entity_refs.users(db, {t.owner_id for t in tasks} | {t.created_by for t in tasks})
    return [
        TaskOut(
            id=t.id,
            title=t.title,
            notes=t.notes,
            owner_id=t.owner_id,
            owner_name=names[t.owner_id].name if t.owner_id in names else None,
            created_by=t.created_by,
            created_by_name=names[t.created_by].name if t.created_by in names else None,
            entity_type=t.entity_type,
            entity_id=t.entity_id,
            due_at=t.due_at,
            due_all_day=t.due_all_day,
            priority=t.priority,
            reminder_at=t.reminder_at,
            reminded_at=t.reminded_at,
            status=t.status,
            completed_at=t.completed_at,
            completed_by=t.completed_by,
            source=t.source,
            workflow_execution_id=t.workflow_execution_id,
            is_overdue=is_overdue(t, tz, now),
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tasks
    ]


async def _out(db: AsyncSession, task: Task) -> TaskOut:
    await db.refresh(task)
    (out,) = await _outs(db, [task])
    return out


# ================================================================ validation
def _field_error(field: str, message: str, code: str) -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": [{"field": field, "message": message}]})


async def _check_owner(db: AsyncSession, owner_id: uuid.UUID) -> None:
    if owner_id not in await entity_refs.active_user_ids(db, [owner_id]):
        raise _field_error("owner_id", "The owner must be an active user", "invalid_owner")


async def _check_entity(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> None:
    if entity_type not in TASK_ENTITY_TYPES:
        raise NotFound("Resource not found")
    await entity_refs.require_readable(ctx, db, entity_type, entity_id)


def _check_reminder(
    reminder_at: datetime | None, due_at: datetime | None, all_day: bool, tz: ZoneInfo
) -> None:
    if reminder_at is None:
        return
    if reminder_at <= _now():
        raise _field_error("reminder_at", "The reminder must be in the future", "invalid_reminder")
    if due_at is not None:
        limit = _end_of_local_day(due_at, tz) if all_day else due_at
        if reminder_at > limit:
            raise _field_error(
                "reminder_at", "The reminder must not be after the due time", "invalid_reminder"
            )


def _assigned(db: AsyncSession, task: Task, actor: uuid.UUID | None) -> None:
    if task.owner_id != actor:
        emit(
            db,
            TaskAssigned(
                tenant_id=task.tenant_id,
                task_id=task.id,
                owner_id=task.owner_id,
                actor_user_id=actor,
                entity_type=task.entity_type,
                entity_id=task.entity_id,
            ),
        )


# ================================================================ commands
async def create_task(
    ctx: RequestContext,
    db: AsyncSession,
    data: TaskCreate,
    *,
    source: str = "manual",
    workflow_execution_id: uuid.UUID | None = None,
) -> TaskOut:
    principal = _principal(ctx)
    check(principal, CREATE)
    owner_id = data.owner_id or acting_user_id(principal)
    if owner_id is None:
        raise ValidationFailed("An assignee is required", details={"fields": {"owner_id": "Required"}})
    await _check_owner(db, owner_id)
    if data.entity_type is not None and data.entity_id is not None:
        await _check_entity(ctx, db, data.entity_type, data.entity_id)
    _check_reminder(data.reminder_at, data.due_at, data.due_all_day, await _tz(db))
    task = Task(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        title=data.title,
        notes=data.notes,
        owner_id=owner_id,
        created_by=acting_user_id(principal),
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        due_at=data.due_at,
        due_all_day=data.due_all_day,
        priority=data.priority,
        reminder_at=data.reminder_at,
        status="open",
        source=source,
        workflow_execution_id=workflow_execution_id,
    )
    db.add(task)
    await db.flush()
    _assigned(db, task, _actor_user(ctx, principal))
    return await _out(db, task)


SYSTEM_TASK_SOURCES = frozenset({"onboarding", "offboarding", "collections", "client_portal", "bgv", "agent"})


async def create_system_task(
    ctx: RequestContext, db: AsyncSession, data: TaskCreate, *, source: str
) -> TaskOut:
    """Epic 13 (R7): a task created by a Phase 2 handler, beat job or portal session -- same validation as
    ``create_task`` without the user-principal check; ``created_by`` is NULL and ``source`` recorded. The owner
    is required (there is no calling user to default to)."""
    if source not in SYSTEM_TASK_SOURCES:
        msg = f"unknown system task source {source!r}"
        raise ValueError(msg)
    if data.owner_id is None:
        raise _field_error("owner_id", "A system task needs an owner", "invalid_owner")
    await _check_owner(db, data.owner_id)
    if data.entity_type is not None and data.entity_type not in TASK_ENTITY_TYPES:
        raise NotFound("Resource not found")
    _check_reminder(data.reminder_at, data.due_at, data.due_all_day, await _tz(db))
    task = Task(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id or entity_refs.session_tenant(db),
        title=data.title,
        notes=data.notes,
        owner_id=data.owner_id,
        created_by=None,
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        due_at=data.due_at,
        due_all_day=data.due_all_day,
        priority=data.priority,
        reminder_at=data.reminder_at,
        status="open",
        source=source,
    )
    db.add(task)
    await db.flush()
    _assigned(db, task, None)
    return await _out(db, task)


async def reassign(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, owner_id: uuid.UUID) -> TaskOut:
    """Epic 32 (FR-170): system re-assignment of an open task (escalations). No user-permission check -- the
    caller is a system job in the tenant session; the new owner must be an active user. Emits ``TaskAssigned``."""
    task = await db.get(Task, task_id)
    if task is None or task.archived_at is not None:
        raise NotFound("Resource not found")
    await _check_owner(db, owner_id)
    if task.owner_id != owner_id:
        task.owner_id = owner_id
        await db.flush()
        _assigned(db, task, None)
    _ = ctx
    return await _out(db, task)


async def get_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> TaskOut:
    return await _out(db, await _get(ctx, db, task_id))


async def update_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, data: TaskUpdate) -> TaskOut:
    principal = _principal(ctx)
    task = await _get(ctx, db, task_id, UPDATE)
    sent = data.model_fields_set
    if "title" in sent and data.title is not None:
        task.title = data.title
    if "notes" in sent:
        task.notes = data.notes
    if "priority" in sent and data.priority is not None:
        task.priority = data.priority
    if {"entity_type", "entity_id"} & sent:
        entity_type = data.entity_type if "entity_type" in sent else task.entity_type
        entity_id = data.entity_id if "entity_id" in sent else task.entity_id
        if (entity_type is None) != (entity_id is None):
            raise _field_error("entity_id", "entity_type and entity_id go together", "validation_failed")
        if entity_type is not None and entity_id is not None:
            await _check_entity(ctx, db, entity_type, entity_id)
        task.entity_type, task.entity_id = entity_type, entity_id
    if "due_at" in sent:
        task.due_at = data.due_at
    if "due_all_day" in sent and data.due_all_day is not None:
        task.due_all_day = data.due_all_day
    if "reminder_at" in sent:
        if data.reminder_at != task.reminder_at:
            task.reminded_at = None
        task.reminder_at = data.reminder_at
    if (
        {"reminder_at", "due_at", "due_all_day"} & sent
        and task.reminder_at is not None
        and task.reminded_at is None
    ):
        _check_reminder(task.reminder_at, task.due_at, task.due_all_day, await _tz(db))
    owner_changed = False
    if "owner_id" in sent and data.owner_id is not None and data.owner_id != task.owner_id:
        await _check_owner(db, data.owner_id)
        task.owner_id = data.owner_id
        owner_changed = True
    await db.flush()
    if owner_changed:
        _assigned(db, task, _actor_user(ctx, principal))
    return await _out(db, task)


async def complete_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> TaskOut:
    principal = _principal(ctx)
    task = await _get(ctx, db, task_id, UPDATE)
    if task.status != "done":
        task.status, task.completed_at, task.completed_by = "done", _now(), acting_user_id(principal)
        await db.flush()
        emit(
            db,
            TaskCompleted(
                tenant_id=task.tenant_id, task_id=task.id, owner_id=task.owner_id, completed_by=acting_user_id(principal)
            ),
        )
    return await _out(db, task)


async def reopen_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> TaskOut:
    task = await _get(ctx, db, task_id, UPDATE)
    if task.status != "open":
        task.status, task.completed_at, task.completed_by = "open", None, None
        await db.flush()
    return await _out(db, task)


async def delete_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> None:
    task = await _get(ctx, db, task_id, DELETE)
    task.archived_at = _now()
    if task.status == "open":
        task.status = "cancelled"
    await db.flush()


# ================================================================ queries
def _me_or_id(value: str, principal: Principal, field: str) -> uuid.UUID:
    if value == "me":
        return principal.id
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise _field_error(field, "Use 'me' or a user id", "validation_failed") from exc


def _apply_filters(stmt: Any, principal: Principal, f: TaskFilters, tz: ZoneInfo, now: datetime) -> Any:
    if f.owner:
        stmt = stmt.where(Task.owner_id == _me_or_id(f.owner, principal, "owner"))
    if f.created_by:
        stmt = stmt.where(Task.created_by == _me_or_id(f.created_by, principal, "created_by"))
    if f.entity_type:
        stmt = stmt.where(Task.entity_type == f.entity_type)
    if f.entity_id:
        stmt = stmt.where(Task.entity_id == f.entity_id)
    if f.q:
        term = "%" + f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(Task.title.ilike(term, escape="\\"), Task.notes.ilike(term, escape="\\")))
    today = _today(tz, now)
    if f.view == "today":
        stmt = stmt.where(Task.status == "open", Task.due_at.is_not(None), _local_date(tz) == today)
    elif f.view == "overdue":
        stmt = stmt.where(_overdue_cond(tz, now))
    elif f.view == "upcoming":
        stmt = stmt.where(Task.status == "open", Task.due_at.is_not(None), _local_date(tz) > today)
    elif f.view == "no_date":
        stmt = stmt.where(Task.status == "open", Task.due_at.is_(None))
    elif f.view == "completed":
        stmt = stmt.where(Task.status == "done")
    return stmt


def _order(stmt: Any, sort: str) -> Any:
    if sort == "-due_at":
        return stmt.order_by(Task.due_at.desc().nulls_last(), Task.id)
    if sort == "priority":
        rank = case(_PRIORITY_RANK, value=Task.priority, else_=9)
        return stmt.order_by(rank, Task.due_at.asc().nulls_last(), Task.id)
    if sort == "created_at":
        return stmt.order_by(Task.created_at, Task.id)
    if sort == "-created_at":
        return stmt.order_by(Task.created_at.desc(), Task.id)
    return stmt.order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id)


async def list_tasks(
    ctx: RequestContext, db: AsyncSession, filters: TaskFilters, params: PageParams
) -> Page[TaskOut]:
    principal = _principal(ctx)
    tz, now = await _tz(db), _now()
    stmt = _order(_apply_filters(_visible(principal), principal, filters, tz, now), filters.sort)
    page = await paginate(db, stmt, params)
    return Page[TaskOut](
        items=await _outs(db, page.items), total=page.total, page=page.page, page_size=page.page_size
    )


async def summary(ctx: RequestContext, db: AsyncSession) -> TaskSummaryOut:
    """The caller's own open tasks: total, due today (local), overdue."""  # noqa: D401
    principal = _principal(ctx)
    tz, now = await _tz(db), _now()
    base = [Task.archived_at.is_(None), Task.owner_id == principal.id, Task.status == "open"]
    today = _today(tz, now)
    row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Task.due_at.is_not(None), _local_date(tz) == today),
                func.count().filter(_overdue_cond(tz, now)),
            ).where(*base)
        )
    ).one()
    return TaskSummaryOut(open=int(row[0]), due_today=int(row[1]), overdue=int(row[2]))


async def entity_tasks(
    ctx: RequestContext, db: AsyncSession, segment: str, entity_id: uuid.UUID, params: PageParams
) -> Page[TaskOut]:
    from app.services import activity_service

    entity_type = activity_service.resolve_entity_type(segment)
    await _check_entity(ctx, db, entity_type, entity_id)
    return await list_tasks(ctx, db, TaskFilters(entity_type=entity_type, entity_id=entity_id), params)  # type: ignore[arg-type]


async def overdue_and_today(
    db: AsyncSession, owner_id: uuid.UUID, tz: ZoneInfo, now: datetime
) -> tuple[list[Task], list[Task]]:
    """For the daily digest: the owner's overdue open tasks and open tasks due today (local)."""
    base = select(Task).where(Task.archived_at.is_(None), Task.owner_id == owner_id, Task.status == "open")
    overdue = list(
        (await db.execute(base.where(_overdue_cond(tz, now)).order_by(Task.due_at).limit(50))).scalars()
    )
    today = list(
        (
            await db.execute(
                base.where(Task.due_at.is_not(None), _local_date(tz) == _today(tz, now))
                .where(Task.id.not_in([t.id for t in overdue]) if overdue else Task.id.is_not(None))
                .order_by(Task.due_at)
                .limit(50)
            )
        ).scalars()
    )
    return overdue, today


# ================================================================ reminders (Celery)
async def send_reminders(db: AsyncSession, now: datetime | None = None) -> int:
    """Claim due reminders (``FOR UPDATE SKIP LOCKED``), stamp ``reminded_at`` and emit ``TaskReminderDue``."""  # noqa: E501
    now = now or _now()
    rows = list(
        (
            await db.execute(
                select(Task)
                .where(
                    Task.status == "open",
                    Task.reminded_at.is_(None),
                    Task.reminder_at.is_not(None),
                    Task.reminder_at <= now,
                    Task.archived_at.is_(None),
                )
                .order_by(Task.reminder_at)
                .limit(REMINDER_BATCH)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    with entity_refs.acting_as(db, "system"):
        for task in rows:
            task.reminded_at = now
        await db.flush()
    for task in rows:
        emit(
            db,
            TaskReminderDue(
                tenant_id=task.tenant_id,
                task_id=task.id,
                owner_id=task.owner_id,
                event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"task-reminder:{task.id}:{task.reminder_at.isoformat() if task.reminder_at else ''}",
                ),
                entity_type=task.entity_type,
                entity_id=task.entity_id,
            ),
        )
    return len(rows)


async def count_overdue_for_entities(
    ctx: RequestContext,  # noqa: ARG001 - uniform service signature
    db: AsyncSession,
    entity_types: Sequence[str],
    entity_ids: Any = None,
    now: datetime | None = None,
) -> int:
    """Epic 15: overdue open tasks linked to records of ``entity_types`` (optionally only ``entity_ids``: a
    sequence or a scalar subquery of ids already narrowed to the caller's data scope). One SQL count."""
    tz = await _tz(db)
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.entity_type.in_(list(entity_types)),
            Task.archived_at.is_(None),
            _overdue_cond(tz, now or _now()),
        )
    )
    if entity_ids is not None:
        stmt = stmt.where(Task.entity_id.in_(entity_ids))
    return int((await db.execute(stmt)).scalar_one())
