"""BGV Celery tasks (Epic 20). Every task starts with ``tenant_id`` (except fan-outs).

* ``bgv.handle_event(tenant_id, name, payload)`` (queue ``interactive``): ``ComplianceRequirementsEvaluated``
  (case creation), ``PlacementDropped`` (cancel), ``ConsentWithdrawn`` (purpose ``bgv``: halt).
* ``bgv.daily_all`` (beat, 02:20 IST) -> ``bgv.daily(tenant_id)`` (queue ``maintenance``): ``bgv.overdue``,
  ``bgv.discrepancy_lapse``, document reminders, reference reminders / expiry and ``bgv.purge_evidence``.
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids


async def _handle(tenant_id: uuid.UUID, name: str, payload: dict[str, Any]) -> int:
    from app.services.bgv import cases, consent_events

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        if name == "ComplianceRequirementsEvaluated":
            return await cases.handle_requirements_evaluated(ctx, db, payload)
        if name == "PlacementDropped":
            return await cases.handle_placement_dropped(ctx, db, uuid.UUID(str(payload["placement_id"])))
        if name == "ConsentWithdrawn" and payload.get("purpose") == "bgv":
            return await consent_events.handle_withdrawn(db, uuid.UUID(str(payload["candidate_id"])))
    return 0


@celery_app.task(name="bgv.handle_event", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def handle_event(tenant_id: str, name: str, payload: dict[str, Any]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _handle(tid, name, payload), tenant_id=tid))


async def _daily(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services.bgv import cases, checks, discrepancies, references, retention

    out: dict[str, int] = {}
    steps = (
        ("overdue", checks.mark_overdue),
        ("lapsed", discrepancies.lapse_due),
        ("doc_reminders", cases.doc_reminders),
    )
    for key, fn in steps:
        try:
            async with tenant_session(tenant_id) as db:
                out[key] = await fn(db)
        except Exception:
            logger.exception("bgv daily step {} failed", key)
    try:
        async with tenant_session(tenant_id) as db:
            out.update(await references.reminders_and_expiry(db))
    except Exception:
        logger.exception("bgv reference reminders failed")
    try:
        async with tenant_session(tenant_id) as db:
            purged = await retention.purge_due(db)
        out["purged_cases"] = purged["cases"]
    except Exception:
        logger.exception("bgv evidence purge failed")
    return out


@celery_app.task(name="bgv.daily", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _daily(tid), tenant_id=tid))


@celery_app.task(name="bgv.daily_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)
