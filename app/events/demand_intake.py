# ruff: file-ignore[line-too-long]
"""Demand Agent events (Epic 55, owner 55; dispatched after commit). Ids, codes and scalars only -- no PII.

``DemandIntakeMatched`` is a subclass of ``DemandIntakeReceived`` emitted instead of it when the message is
``matched``: handlers of ``DemandIntakeReceived`` (workflow, notifications) receive both (``core.events`` dispatches
along the MRO), while the Demand Agent's ``intake_received`` trigger subscribes to the matched kind only -- the
foundation ``TriggerSpec`` has no event filter.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class DemandIntakeReceived:
    tenant_id: uuid.UUID
    message_id: uuid.UUID
    source: str
    status: str
    matched_client_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    # Bumped by ``retry`` so a retried message starts a new run (dedup key ``intake:{message_id}:{attempt}``).
    attempt: int


@dataclasses.dataclass(frozen=True, slots=True)
class DemandIntakeMatched(DemandIntakeReceived):
    """A ``matched`` intake message (the agent trigger)."""


@dataclasses.dataclass(frozen=True, slots=True)
class DemandDuplicateFlagged:
    tenant_id: uuid.UUID
    flag_id: uuid.UUID
    demand_id: uuid.UUID
    related_demand_id: uuid.UUID
    score: str  # decimal string, e.g. "0.812" (the event catalogue allows no Decimal)


@dataclasses.dataclass(frozen=True, slots=True)
class DemandFlagResolved:
    tenant_id: uuid.UUID
    flag_id: uuid.UUID
    demand_id: uuid.UUID
    kind: str
    resolution: str
    resolved_by: uuid.UUID


__all__ = ["DemandDuplicateFlagged", "DemandFlagResolved", "DemandIntakeMatched", "DemandIntakeReceived"]
