# ruff: noqa: E501
"""Public Notes, Activities and Tasks (Epic 67, spec A4).

* Notes / activities: the record must be readable (``activity_service.require_entity``: the owner's checker); private
  notes of others stay hidden (``activity:read_private``); writes go through ``activity_service.add_activity`` (stored as
  untrusted user text, never sent). Public activity kinds map to the P1 user kinds: ``call`` / ``meeting`` as is,
  ``email_logged`` / ``other`` as a ``note`` (the summary is prefixed with the kind).
* Tasks: ``tasks_service`` (owner / creator scope); an app token (no user) must name ``assignee_id``.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import acting_user_id
from app.core.errors import ValidationFailed
from app.core.permissions import scope_condition
from app.models.activity import Activity
from app.models.tasks import Task
from app.schemas.activity import ActivityCreate
from app.schemas.public_v1.resources import ActivityCreateIn, NoteCreateIn, TaskCreateIn, TaskUpdateIn
from app.schemas.tasks import TaskCreate, TaskUpdate
from app.services import activity_service, tasks_service
from app.services.public_api._core import Order, base, ctx_principal, finish_all, one, page, version_of

NOTE_KINDS = ("note",)
ACTIVITY_KINDS = ("note", "call", "meeting")


# ================================================================ notes / activities
def _visible_activities(kinds: Sequence[str]) -> Any:
    _, principal = ctx_principal()
    stmt = select(Activity).where(Activity.kind.in_(kinds))
    if not principal.has("activity:read_private"):
        me = acting_user_id(principal)
        stmt = stmt.where(or_(Activity.is_private.is_(False), and_(Activity.author_id.is_not(None), Activity.author_id == me)))
    return stmt


async def _build_activities(rows: Sequence[Activity]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(a, "note" if a.kind == "note" else "activity"), "updated_at": a.edited_at or a.created_at,
         "entity_type": a.entity_type, "entity_id": a.entity_id, "kind": a.kind, "body": a.body,
         "occurred_at": a.occurred_at, "author_id": a.author_id}
        for a in rows
    ])  # fmt: skip


async def list_entries(db: AsyncSession, *, kinds: Sequence[str], entity_type: str, entity_id: uuid.UUID, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    resolved = activity_service.resolve_entity_type(entity_type)
    await activity_service.require_entity(ctx, db, resolved, entity_id)
    stmt = _visible_activities(kinds).where(Activity.entity_type == resolved, Activity.entity_id == entity_id)
    return await page(db, stmt, sort_col=Activity.created_at, id_col=Activity.id, limit=limit, cursor=cursor, order=order, build=_build_activities)


async def _add(db: AsyncSession, entity_type: str, entity_id: uuid.UUID, body: ActivityCreate) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    out = await activity_service.add_activity(ctx, db, entity_type, entity_id, body)
    row = await db.get(Activity, out.id)
    assert row is not None
    return (await _build_activities([row]))[0]


async def create_note(db: AsyncSession, data: NoteCreateIn) -> dict[str, Any]:
    return await _add(db, data.entity_type, data.entity_id, ActivityCreate(kind="note", body=data.body))


async def create_activity(db: AsyncSession, data: ActivityCreateIn) -> dict[str, Any]:
    if data.kind in ("call", "meeting"):
        body = ActivityCreate(kind=data.kind, body=data.summary, occurred_at=data.occurred_at)
    else:
        label = "Email logged" if data.kind == "email_logged" else "Activity"
        body = ActivityCreate(kind="note", body=f"[{label}] {data.summary}", occurred_at=data.occurred_at)
    return await _add(db, data.entity_type, data.entity_id, body)


# ================================================================ tasks
def _visible_tasks() -> Any:
    _, principal = ctx_principal()
    stmt = select(Task).where(Task.archived_at.is_(None))
    by_owner = scope_condition(principal, "task:read", Task.owner_id)
    by_creator = scope_condition(principal, "task:read", Task.created_by)
    if by_owner is None or by_creator is None:
        return stmt
    return stmt.where(or_(by_owner, by_creator))


async def _build_tasks(rows: Sequence[Task]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(t, "task"), "title": t.title, "notes": t.notes, "assignee_id": t.owner_id, "entity_type": t.entity_type,
         "entity_id": t.entity_id, "due_at": t.due_at, "priority": t.priority, "status": t.status, "completed_at": t.completed_at}
        for t in rows
    ])  # fmt: skip


async def list_tasks(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, assignee_id: uuid.UUID | None = None,
                     status: str | None = None, due_before: datetime | None = None, entity: str | None = None) -> dict[str, Any]:  # fmt: skip
    stmt = _visible_tasks()
    if assignee_id is not None:
        stmt = stmt.where(Task.owner_id == assignee_id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if due_before is not None:
        stmt = stmt.where(Task.due_at < due_before)
    if entity:
        etype, _, eid = entity.partition(":")
        try:
            stmt = stmt.where(Task.entity_type == etype, Task.entity_id == uuid.UUID(eid))
        except ValueError as exc:
            raise ValidationFailed("entity must be <type>:<id>", details={"fields": {"entity": "<type>:<uuid>"}}) from exc
    return await page(db, stmt, sort_col=Task.updated_at, id_col=Task.id, limit=limit, cursor=cursor, order=order, build=_build_tasks)


async def _task(db: AsyncSession, task_id: uuid.UUID) -> Task:
    return await one(db, _visible_tasks().where(Task.id == task_id))


async def get_task(db: AsyncSession, task_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_tasks([await _task(db, task_id)]))[0]


async def task_version(db: AsyncSession, task_id: uuid.UUID) -> str:
    return version_of(await _task(db, task_id))


async def create_task(db: AsyncSession, data: TaskCreateIn) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    out = await tasks_service.create_task(ctx, db, TaskCreate(
        title=data.title, notes=data.notes, owner_id=data.assignee_id, entity_type=data.entity_type,  # type: ignore[arg-type]
        entity_id=data.entity_id, due_at=data.due_at, priority=data.priority,
    ))  # fmt: skip
    return await get_task(db, out.id)


async def update_task(db: AsyncSession, task_id: uuid.UUID, data: TaskUpdateIn) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    await _task(db, task_id)
    sent = data.model_dump(exclude_unset=True)
    status = sent.pop("status", None)
    if "assignee_id" in sent:
        sent["owner_id"] = sent.pop("assignee_id")
    if sent:
        await tasks_service.update_task(ctx, db, task_id, TaskUpdate.model_validate(sent))
    if status == "done":
        await tasks_service.complete_task(ctx, db, task_id)
    elif status == "open":
        await tasks_service.reopen_task(ctx, db, task_id)
    await db.flush()
    row = await _task(db, task_id)
    await db.refresh(row)
    return (await _build_tasks([row]))[0]


__all__ = ["create_activity", "create_note", "create_task", "get_task", "list_entries", "list_tasks", "task_version", "update_task"]
