"""Domain-local (not frozen) after-commit events of the ``talent_pools`` domain (Epic 38).

``PoolRefreshRequested`` starts the refresh task after the request's transaction commits (the task's own
session must see the ``running`` refresh row). The frozen, cross-domain ``PoolMembershipChanged`` lives in
``app/events/talent_pools.py``.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class PoolRefreshRequested:
    tenant_id: uuid.UUID
    pool_id: uuid.UUID
    refresh_id: uuid.UUID
    trigger: str
