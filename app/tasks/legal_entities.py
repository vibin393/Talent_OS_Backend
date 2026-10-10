"""Legal Entities Celery tasks (Epic 13, AD-22). Every task starts with ``tenant_id`` (except fan-outs).

* ``legal_entities.ensure_periods_all`` (beat, daily 00:30 IST) -> ``legal_entities.ensure_periods(tenant_id)``:
  open accounting periods through the current month (entity timezone) + 2 for every active entity.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _ensure(tenant_id: uuid.UUID) -> int:
    from sqlalchemy import select

    from app.models.legal_entities import LegalEntity
    from app.services.legal_entities import periods

    created = 0
    async with tenant_session(tenant_id) as db:
        rows = (await db.execute(select(LegalEntity.id, LegalEntity.timezone).where(LegalEntity.status == "active"))).all()
        for entity_id, tz in rows:
            created += await periods.ensure_window(db, entity_id, tz)
    return created


@celery_app.task(name="legal_entities.ensure_periods", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_periods(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _ensure(tid), tenant_id=tid))


@celery_app.task(name="legal_entities.ensure_periods_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_periods_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(ensure_periods, str(tid))
    return len(ids)
