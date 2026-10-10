"""Client Portal Celery tasks (Epic 18; AD-9 tenant-scoped and idempotent, queue ``interactive``).

* ``client_portal.issue_approval_link(tenant_id, request_id, client_user_id)`` -- after a Client User step activates;
* ``client_portal.revoke_approval_links(tenant_id, request_id)`` -- after a request completes or moves on.
"""

import uuid

from app.core.celery import celery_app
from app.tasks.runtime import run

QUEUE = "interactive"


@celery_app.task(name="client_portal.issue_approval_link", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def issue_approval_link(tenant_id: str, request_id: str, client_user_id: str) -> bool:
    from app.services.client_portal import links

    tid = uuid.UUID(str(tenant_id))
    token = run(
        lambda: links.issue_for_step(tid, uuid.UUID(request_id), uuid.UUID(client_user_id)), tenant_id=tid
    )
    return token is not None


@celery_app.task(name="client_portal.revoke_approval_links", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def revoke_approval_links(tenant_id: str, request_id: str) -> int:
    from app.services.client_portal import links

    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: links.revoke_for_request(tid, uuid.UUID(request_id)), tenant_id=tid))
