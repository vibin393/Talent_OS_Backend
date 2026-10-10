# ruff: noqa: E501
"""Ecosystem governance Celery tasks (Epic 79). Per-tenant tasks start with ``tenant_id``.

* ``ecosystem_governance.fan_out_erasure(tenant_id, dsr_id)`` (queue ``ecosystem``) -- DSR erasure downstream fan-out.
* ``ecosystem_governance.open_reviews`` (beat, daily, ``maintenance``) -- open a review when the cadence is due, then
  auto-revoke stale undecided items for tenants that opted in.
* ``ecosystem_governance.sandbox_purge`` (beat, daily, ``maintenance``) -- warn then crypto-shred inactive sandboxes.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "ecosystem"
MAINTENANCE = "maintenance"


@celery_app.task(name="ecosystem_governance.fan_out_erasure", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def fan_out_erasure(tenant_id: str, dsr_id: str) -> None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> None:
        from app.services.ecosystem_governance import erasure_fanout
        from app.services.privacy import dsr as dsr_service  # resolve the subject from the DSR

        async with tenant_session(tid) as db:
            subject = await _resolve_subject(db, dsr_service, uuid.UUID(str(dsr_id)))
            if subject is None:
                return
            subject_type, subject_id = subject
            await erasure_fanout.fan_out(
                db, tid, uuid.UUID(str(dsr_id)), subject_type=subject_type, subject_id=subject_id
            )

    run(job, tenant_id=tid)


async def _resolve_subject(db: Any, dsr_service: Any, dsr_id: uuid.UUID) -> tuple[str, uuid.UUID] | None:
    """Best-effort: read the DSR row to find its subject (Candidate / Worker)."""
    from sqlalchemy import text

    try:
        row = (
            await db.execute(
                text("SELECT subject_type, subject_id FROM data_subject_requests WHERE id = :id"),
                {"id": dsr_id},
            )
        ).first()
        if row and row[1]:
            return str(row[0] or "candidate"), uuid.UUID(str(row[1]))
    except Exception:  # pragma: no cover - column / table shape differs
        return None
    return None


@celery_app.task(name="ecosystem_governance.open_reviews", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def open_reviews() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.ecosystem_governance import access_reviews

        async with tenant_session(tid) as db:
            opened = 0
            if await access_reviews.cadence_due(db, tid):
                await access_reviews.open_review(db, tid)
                opened = 1
            await access_reviews.auto_revoke_due(db, tid)
            return opened

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="ecosystem_governance.sandbox_purge", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sandbox_purge() -> dict[str, Any]:
    async def job() -> dict[str, Any]:
        from app.services.ecosystem_governance import sandbox_purge as sp

        return await sp.purge()

    return run(job)  # type: ignore[no-any-return]


__all__ = ["fan_out_erasure", "open_reviews", "sandbox_purge"]
