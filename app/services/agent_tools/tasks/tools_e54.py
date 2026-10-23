# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tool ``tasks.list`` (FR-277): tasks you can see (``tasks_service.list_tasks``). It never
completes, reassigns or deletes a task (``tasks.create`` drafts are a separate, confirmed tool)."""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.pagination import PageParams
from app.schemas.tasks import TaskEntity
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, scoped


class TaskListIn(In):
    mine: bool = True
    overdue: bool = False
    entity_type: TaskEntity | None = None
    entity_id: uuid.UUID | None = None
    limit: int = Field(default=10, ge=1, le=25)


class TaskRow(Out):
    id: uuid.UUID
    title: str
    due_at: str | None
    priority: str
    status: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    owner_id: uuid.UUID | None


class TaskListOut(Out):
    total: int
    rows: list[TaskRow]


async def _list(tctx: Any, args: TaskListIn) -> ToolOutcome:
    from app.schemas.tasks import TaskFilters
    from app.services import tasks_service

    filters = TaskFilters(
        owner="me" if args.mine else None,
        view="overdue" if args.overdue else None,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
    )
    page = await tasks_service.list_tasks(
        scoped(tctx), tctx.db, filters, PageParams(page=1, page_size=args.limit)
    )
    rows = [
        TaskRow(
            id=t.id, title=t.title[:200], due_at=t.due_at.isoformat() if t.due_at else None, priority=t.priority,
            status=t.status, entity_type=t.entity_type, entity_id=t.entity_id, owner_id=t.owner_id,
        )
        for t in page.items
    ]  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("task", r.id) for r in rows]
    refs += [(str(r.entity_type), r.entity_id) for r in rows if r.entity_type and r.entity_id]
    return ToolOutcome(
        TaskListOut(total=page.total, rows=rows), refs=tuple(refs), summary=plural(page.total, "task")
    )


register_tool(
    ToolSpec(
        key="tasks.list",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Listed tasks",
        description=(
            "List tasks you can see: your own by default (mine=false widens to tasks visible to you), optionally only "
            "overdue ones or those linked to one record. It never completes, reassigns or deletes a task."
        ),
        input_model=TaskListIn,
        output_model=TaskListOut,
        permissions=("task:read",),
        risk="read",
        scope=ScopeRule(resource="none"),
        handler=_list,
        untrusted_output=frozenset({"rows.title"}),
    )
)
