"""Registrations of the ``payroll`` domain (autodiscovered by ``core.hooks``)."""

import uuid
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Epic 17: vendor payable statement files (PDF / CSV) follow the statement's access rule.
from app.services import files_service
from app.services.payroll import vendor_statements

files_service.register_access_checker(
    "vendor_statement", vendor_statements.file_access, plural="vendor-statements"
)


# ---------------------------------------------------------------- Epic 28: payroll export
def _register_files() -> None:
    from app.services.payroll import files

    files_service.register_access_checker(
        files.FILE_OWNER, files.file_access, plural="payroll-export-versions"
    )


async def _unposted_worker_items(db: AsyncSession, query: Any) -> list[Any]:
    from app.core.context import RequestContext
    from app.services import postings

    ctx = RequestContext(tenant_id=uuid.UUID(str(db.sync_session.info["tenant_id"])), actor_kind="system")
    return await postings.load_unposted(ctx, db, query)


async def _payroll_unfinalized(
    ctx: Any, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date
) -> Any:
    """Close check: Open pay periods ending in the month that still have approved unposted payable items."""
    from app.models.payroll import PayPeriod
    from app.services import postings
    from app.services.legal_entities.periods import CloseCheckResult

    periods = list(
        (
            await db.execute(
                select(PayPeriod).where(
                    PayPeriod.legal_entity_id == legal_entity_id,
                    PayPeriod.status == "open",
                    PayPeriod.period_end >= start,
                    PayPeriod.period_end <= end,
                )
            )
        ).scalars()
    )
    count = 0
    for p in periods:
        items = await postings.load_unposted(
            ctx,
            db,
            postings.SourceQuery(
                ledger="payable",
                period_start=p.period_start,
                period_end=p.period_end,
                legal_entity_id=legal_entity_id,
                payee_kind="worker",
            ),
        )
        count += 1 if items else 0
    return CloseCheckResult(
        name="payroll_unfinalized",
        label="Pay periods not finalized",
        blocking=False,
        count=count,
        link_path="/payroll",
    )


async def _final_payroll_inclusion(db: AsyncSession, placement_id: uuid.UUID, last_working_day: date) -> Any:
    """Offboarding verifier (R18): every approved payable item up to the last working day is posted."""
    from app.services import postings
    from app.services.placements_service import VerifierResult

    items = await _unposted_worker_items(
        db,
        postings.SourceQuery(
            ledger="payable", period_end=last_working_day, placement_ids=[placement_id], payee_kind="worker"
        ),
    )
    return VerifierResult(done=not items, details={"unposted_items": len(items)})


def _register_platform() -> None:
    from app.services.legal_entities.periods import close_checks, register_close_check
    from app.services.placements_service import offboarding_verifiers, register_offboarding_verifier

    if "payroll_unfinalized" not in close_checks():
        register_close_check("payroll_unfinalized", _payroll_unfinalized)
    if "final_payroll_inclusion" not in offboarding_verifiers():
        register_offboarding_verifier("final_payroll_inclusion", _final_payroll_inclusion)


for _fn in (_register_files, _register_platform):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block other domains' registrations
        logger.exception("payroll registration {} failed", _fn.__name__)
