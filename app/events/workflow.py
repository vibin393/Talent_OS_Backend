"""Workflow events (Epic 10, dispatched after commit). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowExecutionFailed:
    """A live rule execution ended ``failed`` or ``partially_failed`` (admins are notified)."""

    tenant_id: uuid.UUID
    execution_id: uuid.UUID
    rule_id: uuid.UUID
    status: str


# ---------------------------------------------------------------- Epic 32: graph runs (AD-31)
@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowWaitCreated:
    tenant_id: uuid.UUID
    wait_id: uuid.UUID
    execution_id: uuid.UUID
    rule_id: uuid.UUID
    kind: str
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowWaitResumed:
    """An approval node was decided (or a wait timed out); the run continues in ``workflow.continue_run``
    after this commit -- never inside the decision transaction."""

    tenant_id: uuid.UUID
    wait_id: uuid.UUID
    execution_id: uuid.UUID
    rule_id: uuid.UUID
    outcome: str
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowRunCompleted:
    tenant_id: uuid.UUID
    execution_id: uuid.UUID
    rule_id: uuid.UUID
    status: str
    origin_chain_depth: int = 0
