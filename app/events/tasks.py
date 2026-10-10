"""Task events (Epic 10, dispatched after commit). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class TaskAssigned:
    """A task got an owner other than the acting user (create or re-assign)."""

    tenant_id: uuid.UUID
    task_id: uuid.UUID
    owner_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    entity_type: str | None
    entity_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class TaskCompleted:
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    owner_id: uuid.UUID
    completed_by: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class TaskReminderDue:
    """Emitted once per task when its ``reminder_at`` passes (``reminded_at`` is set in the same commit)."""

    tenant_id: uuid.UUID
    task_id: uuid.UUID
    owner_id: uuid.UUID
    event_id: uuid.UUID
    entity_type: str | None
    entity_id: uuid.UUID | None
