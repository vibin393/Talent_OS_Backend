"""Task endpoints (Epic 10, story 10.2): ``/tasks`` CRUD, complete / reopen, summary, and the generic
``GET /{entity}/{id}/tasks`` (mounted before the activity router). Each endpoint calls one service function."""  # ruff: ignore[line-too-long]

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.tasks import TaskCreate, TaskFilters, TaskOut, TaskSummaryOut, TaskUpdate
from app.services import tasks_service as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])
entity_router = APIRouter(tags=["tasks"])

Read = Annotated[Principal, Depends(require("task:read"))]
Create = Annotated[Principal, Depends(require("task:create"))]
Update = Annotated[Principal, Depends(require("task:update"))]
Delete = Annotated[Principal, Depends(require("task:delete"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep
EntitySegment = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]


@router.get("", response_model=Page[TaskOut])
async def list_tasks(_: Read, db: DB, filters: Annotated[TaskFilters, Query()], params: Paging) -> Any:
    return await svc.list_tasks(get_context(), db, filters, params)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, _: Create, db: DB) -> Any:
    return await svc.create_task(get_context(), db, body)


@router.get("/summary", response_model=TaskSummaryOut)
async def task_summary(_: Read, db: DB, response: Response) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await svc.summary(get_context(), db)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_task(get_context(), db, task_id)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, body: TaskUpdate, _: Update, db: DB) -> Any:
    return await svc.update_task(get_context(), db, task_id, body)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, _: Delete, db: DB) -> Response:
    await svc.delete_task(get_context(), db, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/complete", response_model=TaskOut)
async def complete_task(task_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.complete_task(get_context(), db, task_id)


@router.post("/{task_id}/reopen", response_model=TaskOut)
async def reopen_task(task_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.reopen_task(get_context(), db, task_id)


@entity_router.get("/{entity}/{entity_id}/tasks", response_model=Page[TaskOut])
async def entity_tasks(entity: EntitySegment, entity_id: uuid.UUID, _: Read, db: DB, params: Paging) -> Any:
    return await svc.entity_tasks(get_context(), db, entity, entity_id, params)
