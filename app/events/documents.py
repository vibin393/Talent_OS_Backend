"""Documents and eSignature (Epic 22).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["SignatureRequestCompleted", "SignatureRequestStatusChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class SignatureRequestStatusChanged:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class SignatureRequestCompleted:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    document_id: uuid.UUID
    final_sha256: str
