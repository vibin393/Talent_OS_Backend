"""After-commit work of the vendors domain (internal, not a contract event): emitted in the request's session
and
dispatched to the Celery task ``vendors.work`` only after the transaction commits, so the task's internal
session sees the committed rows (same pattern as ``documents.esign.EsignWork``)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class VendorWork:
    tenant_id: uuid.UUID
    action: str  # after_submission | resume_revised
    ref_id: uuid.UUID
