"""Domain-local events of the ``expenses`` domain. The foundation events (``ExpenseClaimSubmitted``,
``ExpenseClaimDecided``) live in ``app.events.expenses``. Frozen, slotted, ``tenant_id`` first, ids only;
dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseLineDecided:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    line_id: uuid.UUID
    step: str
    decision: str
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimReimbursed:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID
    export_version_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimStated:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID
    vendor_statement_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimRecalled:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimReopened:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID
