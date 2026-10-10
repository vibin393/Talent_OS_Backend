# ruff: noqa: E501
"""``/public/v1/tasks`` (Epic 67, spec A4): an app token (no user) must name ``assignee_id``."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query, Request, status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, guarded_update, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import TaskCreateIn, TaskOut, TaskUpdateIn
from app.services import public_api_service as svc

router = public_router("tasks")
Read = scope("tasks:read")
Write = scope("tasks:write")


@router.get("/tasks", response_model=Page[TaskOut])
async def list_tasks(
    _: Read, db: PublicDBSessionDep, page: Listing, assignee_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None, due_before: datetime | None = None,
    entity: Annotated[str | None, Query(max_length=80, description="<entity_type>:<id>")] = None,
) -> dict[str, Any]:  # fmt: skip
    return await svc.engagement.list_tasks(db, **page.kw(), assignee_id=assignee_id, status=status_, due_before=due_before, entity=entity)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.engagement.get_task(db, task_id)


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreateIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.engagement.create_task(db, body))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, body: TaskUpdateIn, request: Request, _: Write, db: PublicDBSessionDep) -> dict[str, Any]:
    return await guarded_update(request, lambda: svc.engagement.task_version(db, task_id), lambda: svc.engagement.update_task(db, task_id, body))
