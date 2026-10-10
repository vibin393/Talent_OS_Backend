# ruff: noqa: E501
"""Domain-local mobility events (Epic 41; the frozen contract event ``InternalTransferConfirmed`` lives in
``app/events/mobility.py``). Ids and short labels only, dispatched after commit; they drive the notification types
``internal_application_submitted`` / ``internal_application_decided``."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class InternalApplicationSubmitted:
    tenant_id: uuid.UUID
    mobility_application_id: uuid.UUID
    demand_id: uuid.UUID
    employee_kind: str
    user_id: uuid.UUID | None
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class InternalApplicationDecided:
    tenant_id: uuid.UUID
    mobility_application_id: uuid.UUID
    demand_id: uuid.UUID
    employee_kind: str
    user_id: uuid.UUID | None
    candidate_id: uuid.UUID
    outcome: str  # approved | rejected
