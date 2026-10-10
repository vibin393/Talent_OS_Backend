"""Domain-local placement events (Epic 19 A10; not part of the frozen foundation catalogue). Ids and enums only,
no PII; dispatched after commit. They drive notifications and workflow triggers."""

import dataclasses
import uuid
from datetime import date


@dataclasses.dataclass(frozen=True, slots=True)
class JoiningConfirmed:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    via: str


@dataclasses.dataclass(frozen=True, slots=True)
class JoiningQuestionRaised:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    recruiter_user_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class DropoutRiskRaised:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    recruiter_user_id: uuid.UUID | None
    rules: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class StartGatesUnmet:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    planned_start_date: date
    unmet: int
    recipient_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class DemandReopenSuggested:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    demand_id: uuid.UUID
    owner_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class BankDetailsChangeRequested:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    bank_account_id: uuid.UUID
    last4: str | None
