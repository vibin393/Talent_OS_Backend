"""External principals: Client Users and Vendor Users (Epics 13 / 14).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["ClientUserAccessChanged", "VendorUserStatusChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class ClientUserAccessChanged:
    tenant_id: uuid.UUID
    client_user_id: uuid.UUID
    client_id: uuid.UUID
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class VendorUserStatusChanged:
    tenant_id: uuid.UUID
    vendor_user_id: uuid.UUID
    vendor_id: uuid.UUID
    status: str
