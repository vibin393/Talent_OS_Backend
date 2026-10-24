# ruff: file-ignore[line-too-long]
"""Registrations of the ``receivables`` domain (autodiscovered by ``core.hooks``; Epic 30).

* Approval subjects ``write_off`` (``invoice:write_off``) and ``period_reopen`` (``period:reopen``); callbacks run
  in the deciding transaction.
* Billing settlement probe (IRN-cancel guard); close checks ``receivables.unallocated_payments`` /
  ``receivables.unapplied_bank_rows`` (warnings); reconciliation checks ``receivables.*``; SLA milestone
  ``(dispute, resolution)``.
* Events: ``CreditNoteIssued`` -> apply the CN (task, idempotent); ``InvoiceIssued`` / ``DebitNoteIssued`` /
  ``InvoiceCancelled`` -> refresh the balance cache row (task).
"""

from typing import Any

from loguru import logger

from app.core.events import subscribe


def _register_approvals() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.receivables import periods_close, write_offs

    register_approval_subject(write_offs.SUBJECT, on_final_decided=write_offs.on_final_decided, permission="invoice:write_off", auto_approve_calls_back=True)
    register_approval_subject(periods_close.SUBJECT, on_final_decided=periods_close.on_final_decided, permission="period:reopen", auto_approve_calls_back=True)


def _register_platform() -> None:
    from app.core.reconciliation import reconciliation_checks, register_reconciliation_check
    from app.services.billing.service import register_settlement_probe
    from app.services.legal_entities.periods import close_checks, register_close_check
    from app.services.receivables import periods_close, reconciliation, settlement

    register_settlement_probe(settlement.has_settlements)
    for name, fn in reconciliation.CHECKS.items():
        if name not in reconciliation_checks():
            register_reconciliation_check(name, fn)
    if "receivables.unallocated_payments" not in close_checks():
        register_close_check("receivables.unallocated_payments", periods_close.unallocated_payments_check)
    if "receivables.unapplied_bank_rows" not in close_checks():
        register_close_check("receivables.unapplied_bank_rows", periods_close.unapplied_bank_rows_check)
    try:
        from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

        if milestone("dispute", "resolution") is None:
            register_sla_milestone("dispute", "resolution", SlaMilestoneDef("Dispute resolved", calendar="business"))
    except Exception:
        logger.debug("receivables: SLA milestone registration skipped")


def _on_credit_note(event: Any) -> None:
    from app.tasks import receivables as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.apply_credit_note, str(event.tenant_id), str(event.credit_note_id))


def _on_document(event: Any) -> None:
    from app.tasks import receivables as tasks
    from app.tasks.runtime import enqueue

    doc_id = getattr(event, "invoice_id", None) or getattr(event, "debit_note_id", None)
    if hasattr(event, "debit_note_id"):
        doc_id = event.debit_note_id
    enqueue(tasks.refresh_balance, str(event.tenant_id), str(doc_id))


def _register_events() -> None:
    from app.events.billing import CreditNoteIssued, DebitNoteIssued, InvoiceCancelled, InvoiceIssued

    subscribe(CreditNoteIssued, _on_credit_note)
    for event_type in (InvoiceIssued, DebitNoteIssued, InvoiceCancelled):
        subscribe(event_type, _on_document)


for _fn in (_register_approvals, _register_platform, _register_events):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block other domains' registrations
        logger.exception("receivables registration {} failed", _fn.__name__)


# ---------------------------------------------------------------- Epic 51 (FR-254): 26AS / AIS reconciliation
def _on_tds_parse_queued(event: Any) -> None:
    from app.tasks import finance_p3 as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.tds_parse, str(event.tenant_id), str(event.run_id))


async def _run_creator(db: Any, payload: Any) -> set[Any]:
    import uuid as _uuid

    from app.models.receivables import TdsReconRun

    try:
        run = await db.get(TdsReconRun, _uuid.UUID(str(payload.get("run_id"))))
    except ValueError:
        return set()
    return {run.created_by} if run is not None and run.created_by else set()


async def _render_recon_ready(db: Any, payload: Any) -> Any:
    import uuid as _uuid

    from app.models.receivables import TdsReconRun
    from app.services.notifications_service import Content

    run = await db.get(TdsReconRun, _uuid.UUID(str(payload.get("run_id"))))
    label = f"{run.statement_kind.upper()} {run.fy} {run.quarter}" if run is not None else "26AS"
    return Content(
        title=f"TDS reconciliation {label} is ready for review"[:300],
        link_path=f"/finance/tds-reconciliation/{payload.get('run_id')}",
    )


async def _recon_file_access(ctx: Any, db: Any, entity_id: Any, write: bool) -> bool:  # ruff: ignore[unused-function-argument]  # NOSONAR
    principal = ctx.principal
    if principal is None:
        return True
    has = getattr(principal, "has", None)
    return bool(has and has("receivables:read"))


def _register_tds_recon() -> None:
    from app.events.receivables import TdsReconRunCompleted
    from app.services import files_service
    from app.services.notifications_service import (
        NOTIFICATION_TYPES,
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.receivables.tds_recon import runs
    from app.services.workflow import catalogue

    catalogue.register_human_only_transitions("tds_recon_match", ["confirmed"])
    subscribe(runs.TdsParseQueued, _on_tds_parse_queued)
    if files_service.entity_type_for_path("tds-recon-runs") is None:
        files_service.register_access_checker(runs.FILE_OWNER, _recon_file_access, plural="tds-recon-runs")
    if "tds_recon_ready" not in NOTIFICATION_TYPES:
        register_notification_type(
            "tds_recon_ready",
            NotificationType(
                "TDS reconciliation ready", "Finance", "in_app", False, _run_creator, _render_recon_ready,
                internal_only=True,
            ),
        )
        register_event_source(TdsReconRunCompleted, "tds_recon_ready")


try:
    _register_tds_recon()
except Exception:  # pragma: no cover - never block other domains' registrations
    logger.exception("receivables registration _register_tds_recon failed")
