"""External access Celery tasks (Epic 14; AD-9: ``tenant_id`` first, idempotent).

* ``revoke_vendor_sessions(tenant_id, vendor_id)`` -- after ``VendorStatusChanged`` to a status other than
  active / onboarding: every Vendor User session of that vendor ends (sessions also re-check the vendor status
  on every request, so access stops at once).
"""

import uuid

from app.core.celery import celery_app
from app.tasks.ops_runtime import run

QUEUE = "interactive"  # AD-37


@celery_app.task(name="external_auth.revoke_vendor_sessions", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def revoke_vendor_sessions(tenant_id: str, vendor_id: str) -> int:
    from app.services.external_auth import handlers

    tid, vid = uuid.UUID(tenant_id), uuid.UUID(vendor_id)
    return run(lambda: handlers.revoke_vendor_sessions(tid, vid), tenant_id=tid)  # type: ignore[no-any-return]
