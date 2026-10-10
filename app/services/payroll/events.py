"""Domain-local events of the payroll export (Epic 28). The foundation events (``PayPeriodLocked``,
``PayPeriodReopened``, ``PayrollExportFinalized``) live in ``app.events.payroll``. Frozen, slotted,
``tenant_id`` first, ids only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class PayrollPreviewGenerated:
    tenant_id: uuid.UUID
    version_id: uuid.UUID
    pay_period_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PayrollExportProcessed:
    tenant_id: uuid.UUID
    version_id: uuid.UUID
