"""Task API models (Epic 10, story 10.2)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints, model_validator

# Keep in sync with TASK_ENTITY_TYPES (the DB CHECK constraint in app/models/tasks.py) — that tuple is the
# source of truth for the valid task-entity set; this Literal mirrors it for schema validation / typing.
TaskEntity = Literal[
    "client", "contact", "demand", "candidate", "application", "lead", "opportunity",  # Epic 15: +lead, opportunity
    "offer", "placement", "project", "deliverable", "immigration_case", "campus_drive", "workforce_plan",  # Epic 36
    "referral", "talent_pool", "campaign",
    "agent_run", "agent_proposal",  # Epic 53
    "installation", "webhook_subscription", "board_posting", "hrms_sync", "ledger_sync", "payment_suggestion",  # Epic 66
    "calendar_change", "network_listing", "mcp_session",
]
Priority = Literal["low", "normal", "high", "urgent"]
View = Literal["today", "overdue", "upcoming", "no_date", "completed"]
Sort = Literal["due_at", "-due_at", "priority", "created_at", "-created_at"]

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Notes = Annotated[str, StringConstraints(max_length=10_000)]


class TaskCreate(BaseModel):
    title: Title
    notes: Notes | None = None
    owner_id: uuid.UUID | None = None  # default: the caller
    entity_type: TaskEntity | None = None
    entity_id: uuid.UUID | None = None
    due_at: datetime | None = None
    due_all_day: bool = False
    priority: Priority = "normal"
    reminder_at: datetime | None = None

    @model_validator(mode="after")
    def _entity_pair(self) -> "TaskCreate":
        if (self.entity_type is None) != (self.entity_id is None):
            msg = "entity_type and entity_id go together"
            raise ValueError(msg)
        return self


class TaskUpdate(BaseModel):
    title: Title | None = None
    notes: Notes | None = None
    owner_id: uuid.UUID | None = None
    entity_type: TaskEntity | None = None
    entity_id: uuid.UUID | None = None
    due_at: datetime | None = None
    due_all_day: bool | None = None
    priority: Priority | None = None
    reminder_at: datetime | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    notes: str | None
    owner_id: uuid.UUID
    owner_name: str | None
    created_by: uuid.UUID | None  # NULL for system tasks (Epic 13)
    created_by_name: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    due_at: datetime | None
    due_all_day: bool
    priority: str
    reminder_at: datetime | None
    reminded_at: datetime | None
    status: str
    completed_at: datetime | None
    completed_by: uuid.UUID | None
    source: str
    workflow_execution_id: uuid.UUID | None
    is_overdue: bool
    created_at: datetime
    updated_at: datetime


class TaskFilters(BaseModel):
    owner: Annotated[str, StringConstraints(max_length=36)] | None = None  # "me" | user id
    created_by: Annotated[str, StringConstraints(max_length=36)] | None = None  # "me" | user id
    view: View | None = None
    entity_type: TaskEntity | None = None
    entity_id: uuid.UUID | None = None
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    sort: Sort = "due_at"


class TaskSummaryOut(BaseModel):
    open: int
    due_today: int
    overdue: int
