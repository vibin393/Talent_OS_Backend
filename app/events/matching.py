"""Matching domain events (Epic 5; dispatched after commit, see ``app.core.events``). Ids / numbers only."""

import dataclasses
import uuid
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class MatchRunCompleted:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    run_id: uuid.UUID
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class RediscoveryCompleted:
    """First scoring run after a demand opened: the funnel for the demand timeline + assignee notification
    (Epic 10 delivers it)."""

    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    run_id: uuid.UUID
    funnel: dict[str, Any]
    assignee_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class MatchDismissed:
    tenant_id: uuid.UUID
    match_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class MatchRestored:
    tenant_id: uuid.UUID
    match_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class MatchAddedToPipeline:
    tenant_id: uuid.UUID
    match_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class MatchRunRequested:
    """A ``running`` run row was committed; the handler enqueues ``matching.tasks.run_for_demand``."""

    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    run_id: uuid.UUID
    trigger: str
