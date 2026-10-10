"""Client commercials: contracts, purchase orders, rate cards (Epic 16).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "ContractCreated",
    "ContractRenewalDue",
    "ContractStatusChanged",
    "PoConsumptionThresholdReached",
    "PoExpiring",
    "PoValidityChanged",
    "RateCardPublished",
    "RateExceptionDecided",
]


@dataclasses.dataclass(frozen=True, slots=True)
class ContractStatusChanged:
    tenant_id: uuid.UUID
    contract_id: uuid.UUID
    client_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    from_status: str
    to_status: str
    end_date: date | None


@dataclasses.dataclass(frozen=True, slots=True)
class PoValidityChanged:
    tenant_id: uuid.UUID
    po_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    valid_from: date
    valid_to: date
    remaining_amount: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class PoConsumptionThresholdReached:
    tenant_id: uuid.UUID
    po_id: uuid.UUID
    client_id: uuid.UUID
    pct: int


@dataclasses.dataclass(frozen=True, slots=True)
class RateCardPublished:
    tenant_id: uuid.UUID
    rate_card_id: uuid.UUID
    client_id: uuid.UUID
    contract_id: uuid.UUID | None
    effective_from: date


# ---------------------------------------------------------------- Epic 16 domain-local events
@dataclasses.dataclass(frozen=True, slots=True)
class ContractCreated:
    tenant_id: uuid.UUID
    contract_id: uuid.UUID
    client_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    contract_type: str


@dataclasses.dataclass(frozen=True, slots=True)
class ContractRenewalDue:
    tenant_id: uuid.UUID
    contract_id: uuid.UUID
    client_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    end_date: date
    lead_days: int


@dataclasses.dataclass(frozen=True, slots=True)
class RateExceptionDecided:
    tenant_id: uuid.UUID
    rate_check_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    outcome: str


@dataclasses.dataclass(frozen=True, slots=True)
class PoExpiring:
    tenant_id: uuid.UUID
    po_id: uuid.UUID
    client_id: uuid.UUID
    valid_to: date
    days_left: int
