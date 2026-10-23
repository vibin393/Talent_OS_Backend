# ruff: file-ignore[line-too-long]
"""Domain-local events of the Scheduling Agent (Epic 59). ``SlotPicked`` is the foundation's (``app.events.scheduling``).
Frozen, slotted; ids and scalars only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class SchedulingRequestChanged:
    """A scheduling request changed status (notifications, the Interview / Application panels)."""

    tenant_id: uuid.UUID
    request_id: uuid.UUID
    application_id: uuid.UUID
    status: str
    round: int
    kind: str = "new"
    interview_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class SchedulingRunNeeded:
    """After commit: hand a candidate pick / counter-proposal / reschedule request to the agent (resume the waiting run,
    else start a continuation run -- coordinator C-12)."""

    tenant_id: uuid.UUID
    request_id: uuid.UUID
    trigger_key: str  # slot_picked | counter_proposed | reschedule_requested | requested
    offer_id: uuid.UUID | None = None


__all__ = ["SchedulingRequestChanged", "SchedulingRunNeeded"]
