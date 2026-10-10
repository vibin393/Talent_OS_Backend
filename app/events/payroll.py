"""Payroll export and vendor statements (Epic 28).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["PayPeriodLocked", "PayPeriodReopened", "PayrollExportFinalized", "VendorStatementGenerated"]


@dataclasses.dataclass(frozen=True, slots=True)
class PayrollExportFinalized:
    tenant_id: uuid.UUID
    export_version_id: uuid.UUID
    pay_period_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PayPeriodLocked:
    tenant_id: uuid.UUID
    pay_period_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PayPeriodReopened:
    tenant_id: uuid.UUID
    pay_period_id: uuid.UUID
    legal_entity_id: uuid.UUID
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class VendorStatementGenerated:
    tenant_id: uuid.UUID
    statement_id: uuid.UUID
    vendor_id: uuid.UUID
    pay_period_id: uuid.UUID
