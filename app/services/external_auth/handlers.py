"""After-commit reactions of the ``external_auth`` domain (Epic 14): vendor deactivation ends Vendor User
sessions. Client User suspension / revocation revokes sessions synchronously in ``access``."""

import uuid

from sqlalchemy import select

from app.core.database import tenant_session
from app.events.vendors import VendorStatusChanged
from app.models.external_auth import VendorUser
from app.services.external_auth import sessions

VENDOR_OK_STATUSES = frozenset({"active", "onboarding"})


def on_vendor_status_changed(event: VendorStatusChanged) -> None:
    if event.to_status in VENDOR_OK_STATUSES:
        return
    from app.tasks import external_auth as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.revoke_vendor_sessions, str(event.tenant_id), str(event.vendor_id))


async def revoke_vendor_sessions(tenant_id: uuid.UUID, vendor_id: uuid.UUID) -> int:
    revoked = 0
    async with tenant_session(tenant_id) as db:
        ids = (
            (await db.execute(select(VendorUser.id).where(VendorUser.vendor_id == vendor_id))).scalars().all()
        )
        for vu_id in ids:
            revoked += await sessions.revoke_all(db, "vendor_user", vu_id)
    return revoked
