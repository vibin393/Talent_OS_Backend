"""Legal Entities, GST registrations and accounting periods (Epic 13, AD-17 / AD-22).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "AccountingPeriodClosed",
    "AccountingPeriodReopened",
    "FxFeedFailed",
    "GstRegistrationChanged",
    "LegalEntityCreated",
    "LegalEntityDeactivated",
]


@dataclasses.dataclass(frozen=True, slots=True)
class LegalEntityCreated:
    tenant_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LegalEntityDeactivated:
    tenant_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class GstRegistrationChanged:
    tenant_id: uuid.UUID
    gst_registration_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AccountingPeriodClosed:
    tenant_id: uuid.UUID
    legal_entity_id: uuid.UUID
    year: int
    month: int
    actor_kind: str
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class AccountingPeriodReopened:
    tenant_id: uuid.UUID
    legal_entity_id: uuid.UUID
    year: int
    month: int
    actor_kind: str
    actor_id: uuid.UUID | None
    reason: str


# Epic 51 (FR-255) domain-local: the daily FX feed fetch failed (``consecutive_days`` failing days in a row).
@dataclasses.dataclass(frozen=True, slots=True)
class FxFeedFailed:
    tenant_id: uuid.UUID
    on_date: date
    consecutive_days: int
