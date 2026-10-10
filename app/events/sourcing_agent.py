# ruff: noqa: E501
"""Sourcing & Matching Agent domain events (Epic 56; dispatched after commit). Ids / scalars only.

* ``AgentShortlistSaved`` -- a new agent shortlist version (notification ``sourcing_shortlist_ready``).
* ``AgentShortlistEntryDecided`` -- the FR-323 selection event: an entry was added to the pipeline, dismissed or
  restored (by a person, or by an approved agent Proposal: ``via_proposal``).
* ``SourcingRerankRequested`` / ``SourcingRefreshDue`` -- emitted by the sourcing domain after it filtered the P1
  ``MatchRunCompleted`` (``requirements_changed`` match runs of open Demands) and by the weekly refresh beat; they are
  the event triggers ``requirements_changed`` / ``weekly_refresh`` of the ``sourcing`` agent.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class AgentShortlistSaved:
    tenant_id: uuid.UUID
    shortlist_id: uuid.UUID
    demand_id: uuid.UUID
    run_id: uuid.UUID | None
    entry_count: int
    new_count: int
    dropped_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class AgentShortlistEntryDecided:
    tenant_id: uuid.UUID
    entry_id: uuid.UUID
    shortlist_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    decision: str  # added_to_pipeline | dismissed | restored
    actor_kind: str
    via_proposal: bool


@dataclasses.dataclass(frozen=True, slots=True)
class SourcingRerankRequested:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    match_run_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class SourcingRefreshDue:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    week: str  # ISO week, e.g. "2026-W41"
