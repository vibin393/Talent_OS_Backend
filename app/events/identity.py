"""Identity domain events (dispatched after commit, see ``app.core.events``)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class TenantProvisioned:
    """A tenant and its first-admin invitation were committed. Later modules seed roles/stages on it."""

    tenant_id: uuid.UUID
    admin_email: str
