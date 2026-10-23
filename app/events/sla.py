# ruff: file-ignore[line-too-long]
"""SLA and escalation events (Epic 32; dispatched after commit). Ids / enums only, no PII."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class SlaClockStarted:
    tenant_id: uuid.UUID
    clock_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    milestone: str


@dataclasses.dataclass(frozen=True, slots=True)
class SlaClockStatusChanged:
    """The evaluator saw a clock change status (``healthy -> at_risk -> breached``); emitted once per change."""

    tenant_id: uuid.UUID
    clock_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    milestone: str
    scope_type: str | None
    scope_id: uuid.UUID | None
    from_status: str | None
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class SlaClockCompleted:
    tenant_id: uuid.UUID
    clock_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    milestone: str
    status: str  # met | breached | cancelled


@dataclasses.dataclass(frozen=True, slots=True)
class EscalationFired:
    """An escalation watch fired (notify / reassign); it never decides anything (FR-170)."""

    tenant_id: uuid.UUID
    watch_id: uuid.UUID
    policy_id: uuid.UUID | None
    subject_kind: str
    subject_id: uuid.UUID
    action: str
    target: str
    fire_number: int
