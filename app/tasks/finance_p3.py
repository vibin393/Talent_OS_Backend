# ruff: noqa: E501
"""Phase 3 finance carry-overs (Epic 51) Celery tasks. Every task starts with ``tenant_id`` (except fan-outs).

* ``finance_p3.dsc_sign(tenant_id, kind, doc_id)`` (``connectors``): render once, sign through the ``dsc`` port.
* ``finance_p3.tds_parse(tenant_id, run_id)`` (``finance_runs``): parse a 26AS / AIS run and propose matches.
* ``finance_p3.fx_feed_fetch_all`` (beat ``fx-feed-fetch``, hourly) -> ``finance_p3.fx_feed_fetch(tenant_id)``.
* ``finance_p3.dsc_pending_digest_all`` (beat ``dsc-pending-digest``, 09:30 IST) -> per tenant digest.
"""

import uuid

from loguru import logger
from sqlalchemy import select

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "connectors"


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


# ---------------------------------------------------------------- DSC
async def _dsc_sign(tenant_id: uuid.UUID, kind: str, doc_id: uuid.UUID) -> str:
    from app.services.billing import dsc

    async with tenant_session(tenant_id) as db:
        return await dsc.sign_document(_ctx(tenant_id), db, kind, doc_id)


@celery_app.task(name="finance_p3.dsc_sign", queue="connectors", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dsc_sign(tenant_id: str, kind: str, doc_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _dsc_sign(tid, kind, uuid.UUID(str(doc_id))), tenant_id=tid))


async def _dsc_digest(tenant_id: uuid.UUID) -> int:
    from app.services.billing import dsc

    async with tenant_session(tenant_id) as db:
        return await dsc.pending_digest(_ctx(tenant_id), db)


@celery_app.task(name="finance_p3.dsc_pending_digest", queue="connectors", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dsc_pending_digest(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _dsc_digest(tid), tenant_id=tid) or 0)


@celery_app.task(name="finance_p3.dsc_pending_digest_all", queue="connectors")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dsc_pending_digest_all() -> int:
    ids = run(tenant_ids) or []
    for tid in ids:
        enqueue(dsc_pending_digest, str(tid))
    return len(ids)


# ---------------------------------------------------------------- 26AS reconciliation
async def _tds_parse(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.models.receivables import TdsReconRun
    from app.services.receivables.tds_recon import runs

    try:
        async with tenant_session(tenant_id) as db:
            return await runs.process_run(_ctx(tenant_id), db, run_id)
    except Exception as exc:
        logger.exception("tds reconciliation run {} failed", run_id)
        async with tenant_session(tenant_id) as db:
            row = (await db.execute(select(TdsReconRun).where(TdsReconRun.id == run_id))).scalar_one_or_none()
            if row is not None and row.status == "parsing":
                row.status = "failed"
                row.error = f"{type(exc).__name__}"[:2000]
        return "failed"


@celery_app.task(name="finance_p3.tds_parse", queue="finance_runs", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tds_parse(tenant_id: str, run_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _tds_parse(tid, uuid.UUID(str(run_id))), tenant_id=tid))


# ---------------------------------------------------------------- FX feed
async def _fx_fetch(tenant_id: uuid.UUID) -> str:
    from app.services.legal_entities import fx_feed

    async with tenant_session(tenant_id) as db:
        return await fx_feed.run_if_due(_ctx(tenant_id), db)


@celery_app.task(name="finance_p3.fx_feed_fetch", queue="connectors", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def fx_feed_fetch(tenant_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _fx_fetch(tid), tenant_id=tid))


@celery_app.task(name="finance_p3.fx_feed_fetch_all", queue="connectors")  # type: ignore[misc,untyped-decorator,unused-ignore]
def fx_feed_fetch_all() -> int:
    ids = run(tenant_ids) or []
    for tid in ids:
        enqueue(fx_feed_fetch, str(tid))
    return len(ids)
