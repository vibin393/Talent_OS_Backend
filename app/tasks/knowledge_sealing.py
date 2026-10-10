# ruff: noqa: E501
"""Onboarding Q&A sealing backfill (Epic 78, story 78.4, FR-404). Queue ``maintenance``.

``knowledge.seal_backfill`` (beat every 15 min): per tenant, batches of ``qa_sealing_batch_size`` (500) through
``knowledge.sealing.backfill`` until no plaintext row is left. The cursor is the data itself (rows that still hold
plaintext, in id order) plus the in-run ``resume_after``, so a crashed run resumes where it stopped. On the first
failure the tenant stops (plaintext kept), ``qa_sealing_failed`` is written to the ops log and platform operators are
emailed. After ``512_03`` dropped the plaintext columns the task is an idempotent no-op.
CLI: ``python -m app.cli knowledge seal-qa --tenant <id> | --all [--verify-only]``.
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"
BATCH = 500
MAX_BATCHES_PER_RUN = 200


async def _notify_ops(tenant_id: uuid.UUID, message_id: uuid.UUID | None) -> None:
    try:
        from sqlalchemy import select

        from app.core.database import platform_session
        from app.models.identity import PlatformOperator
        from app.services.privacy.common import email_users

        async with platform_session() as pdb:
            operators = [{"email": e} for e in (await pdb.execute(select(PlatformOperator.email).where(PlatformOperator.is_active.is_(True)))).scalars()]
        text = f"Sealing onboarding Q&A messages stopped for tenant {tenant_id} after message {message_id}. The plaintext was kept; check the tenant data key.\n"
        await email_users(operators, "Onboarding Q&A sealing failed", text, tenant_id=tenant_id, kind="qa_sealing_failed")
    except Exception:
        logger.warning("qa_sealing_failed alert not sent for tenant {}", tenant_id)


async def seal_tenant(tenant_id: uuid.UUID, *, verify_only: bool = False, batch: int = BATCH) -> dict[str, Any]:
    from app.services.knowledge import sealing

    sealed = failed = 0
    last: uuid.UUID | None = None
    touched: list[uuid.UUID] = []
    if not verify_only:
        for _ in range(MAX_BATCHES_PER_RUN):
            async with tenant_session(tenant_id) as db:
                result = await sealing.backfill(db, tenant_id, batch=batch, resume_after=last)
            sealed += result.sealed
            if result.last_id is not None and result.last_id != last:
                touched.append(result.last_id)
                last = result.last_id
            if result.failed:
                failed = result.failed
                logger.error("qa_sealing_failed tenant={} after={}", tenant_id, last)
                await _notify_ops(tenant_id, last)
                break
            if result.sealed + result.skipped < batch:
                break
    if not verify_only and sealed == 0 and failed == 0:
        return {"sealed": 0, "failed": 0, "plaintext_rows": 0, "undecryptable_rows": 0}  # nothing left: idempotent no-op
    async with tenant_session(tenant_id) as db:
        check = await sealing.verify(db, tenant_id, recent=touched)
    return {"sealed": sealed, "failed": failed, "plaintext_rows": check.plaintext_rows, "undecryptable_rows": check.undecryptable_rows}


@celery_app.task(name="knowledge.seal_backfill", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def seal_backfill() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> dict[str, Any]:
        return await seal_tenant(tid)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["seal_backfill", "seal_tenant"]
