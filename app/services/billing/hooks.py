# ruff: noqa: E501, ARG001
"""Registrations of the ``billing`` domain (autodiscovered by ``core.hooks``; Epic 29).

* Approval subjects ``invoice`` / ``credit_note`` / ``debit_note`` (callbacks in the deciding transaction).
* Posting sources ``schedule_item`` and ``manual_item`` (billing ledger, pull-only).
* Events: ``InvoiceRunQueued`` -> build task; ``PlacementCreated`` -> default Billing Rule; ``PlacementConverted``
  -> conversion-fee schedule item; ``RateSegmentAdded`` -> drafts holding the placement's items marked stale.
* Close checks ``billing.draft_documents_in_period`` / ``billing.pending_irn_in_period``; reconciliation checks
  ``billing.*``; SLA milestone ``(invoice, issue_after_period)``; offboarding verifier ``final_invoice``.
"""

import uuid
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe


def _register_approvals() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.billing import issue

    for kind in ("invoice", "credit_note", "debit_note"):
        register_approval_subject(
            kind,
            on_final_decided=issue.on_final_decided,
            permission="invoice:issue",
            auto_approve_calls_back=True,
        )


def _register_sources() -> None:
    from app.services.billing import rules

    rules.register_sources()


# ---------------------------------------------------------------- events (after commit -> tasks)
def _on_run_queued(event: Any) -> None:
    from app.tasks import billing as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.build_invoice_run, str(event.tenant_id), str(event.run_id))


def _on_placement_created(event: Any) -> None:
    from app.tasks import billing as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.placement_created, str(event.tenant_id), str(event.placement_id))


def _on_placement_converted(event: Any) -> None:
    from app.tasks import billing as tasks
    from app.tasks.runtime import enqueue

    enqueue(
        tasks.placement_converted,
        str(event.tenant_id),
        str(event.placement_id),
        event.conversion_date.isoformat(),
        event.fee_amount,
        event.currency,
    )


def _on_rate_segment(event: Any) -> None:
    from app.tasks import billing as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.mark_stale, str(event.tenant_id), str(event.placement_id))


def _register_events() -> None:
    from app.events.placements import PlacementConverted, PlacementCreated, RateSegmentAdded
    from app.services.billing.runs import InvoiceRunQueued

    subscribe(InvoiceRunQueued, _on_run_queued)
    subscribe(PlacementCreated, _on_placement_created)
    subscribe(PlacementConverted, _on_placement_converted)
    subscribe(RateSegmentAdded, _on_rate_segment)


async def mark_stale(db: AsyncSession, placement_id: uuid.UUID) -> int:
    from app.models.billing import Invoice, InvoiceLine

    ids = select(InvoiceLine.invoice_id).where(InvoiceLine.placement_id == placement_id)
    result = await db.execute(
        update(Invoice)
        .where(Invoice.id.in_(ids), Invoice.status.in_(("draft", "pending_approval")))
        .values(stale=True)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- platform registrations
async def _draft_check(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date
) -> Any:
    from app.models.billing import Invoice
    from app.services.legal_entities.periods import CloseCheckResult

    count = (
        await db.execute(
            select(func.count(Invoice.id)).where(
                Invoice.legal_entity_id == legal_entity_id,
                Invoice.status.in_(("draft", "pending_approval")),
                Invoice.preview_date >= start,
                Invoice.preview_date <= end,
            )
        )
    ).scalar() or 0
    return CloseCheckResult(
        "billing.draft_documents_in_period",
        "Draft invoices in the period",
        False,
        int(count),
        "/billing/invoices?status=draft",
    )


async def _pending_irn_check(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date
) -> Any:
    from app.services.billing.common import DOC_KINDS, MODELS
    from app.services.legal_entities.periods import CloseCheckResult

    total = 0
    for kind in DOC_KINDS:
        model = MODELS[kind][0]
        total += int(
            (
                await db.execute(
                    select(func.count(model.id)).where(
                        model.legal_entity_id == legal_entity_id,
                        model.einvoice_status == "pending_irn",
                        model.doc_date >= start,
                        model.doc_date <= end,
                    )
                )
            ).scalar()
            or 0
        )
    return CloseCheckResult(
        "billing.pending_irn_in_period", "Documents waiting for an IRN", True, total, "/billing/einvoice"
    )


async def _final_invoice(db: AsyncSession, placement_id: uuid.UUID, last_working_day: date) -> Any:
    from app.models.billing import Invoice, InvoiceLine
    from app.services.placements_service import VerifierResult

    latest = (
        await db.execute(
            select(func.max(InvoiceLine.period_end))
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(InvoiceLine.placement_id == placement_id, Invoice.status == "issued")
        )
    ).scalar()
    done = latest is not None and latest >= last_working_day
    return VerifierResult(done=done, details={"billed_through": latest.isoformat() if latest else None})


def _register_platform() -> None:
    from app.core.reconciliation import reconciliation_checks, register_reconciliation_check
    from app.services.billing import integrity
    from app.services.legal_entities.periods import close_checks, register_close_check
    from app.services.placements_service import offboarding_verifiers, register_offboarding_verifier
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

    for name, fn in integrity.CHECKS.items():
        if name not in reconciliation_checks():
            register_reconciliation_check(name, fn)
    if "billing.draft_documents_in_period" not in close_checks():
        register_close_check("billing.draft_documents_in_period", _draft_check)
    if "billing.pending_irn_in_period" not in close_checks():
        register_close_check("billing.pending_irn_in_period", _pending_irn_check)
    if "final_invoice" not in offboarding_verifiers():
        register_offboarding_verifier("final_invoice", _final_invoice)
    if milestone("invoice", "issue_after_period") is None:
        register_sla_milestone(
            "invoice",
            "issue_after_period",
            SlaMilestoneDef("Invoice issued after the billing period", calendar="calendar"),
        )


for _fn in (_register_approvals, _register_sources, _register_events, _register_platform):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block other domains' registrations
        logger.exception("billing registration {} failed", _fn.__name__)


# ---------------------------------------------------------------- Epic 51 (FR-253): DSC signing
def _dsc_task() -> Any:
    # Resolved at call time: the shared Epic 51 task module also hosts the receivables (26AS) task, and billing must
    # not depend on receivables (p2-dag); only the ``finance_p3.dsc_sign`` task is used here.
    import importlib

    return importlib.import_module("app.tasks.finance_p3").dsc_sign


def _on_doc_issued_dsc(event: Any) -> None:
    from app.services.billing import dsc
    from app.tasks.runtime import enqueue

    kind = "credit_note" if hasattr(event, "credit_note_id") else "invoice"
    doc_id = getattr(event, "credit_note_id", None) or event.invoice_id
    if not dsc.take_marked(event.tenant_id, kind, doc_id):
        return  # no required DSC policy at issue (dsc_status stays not_required)
    enqueue(_dsc_task(), str(event.tenant_id), kind, str(doc_id))


def _on_dsc_queued(event: Any) -> None:
    from app.tasks.runtime import enqueue

    enqueue(_dsc_task(), str(event.tenant_id), event.kind, str(event.doc_id))


async def _dsc_admins(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.services import identity_bridge

    return {u["id"] for u in await identity_bridge.users_with_permission(db, "dsc:configure")}


async def _render_dsc_pending(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    kind = str(payload.get("kind") or "invoice")
    label = "Credit note" if kind == "credit_note" else "Invoice"
    number = payload.get("number") or ""
    failed = payload.get("status") == "failed"
    title = (
        f"{label} {number}: DSC signing failed -- retry or upload the signed PDF"
        if failed
        else f"{label} {number} is waiting for its DSC signature ({payload.get('age_hours', 0)} h)"
    )
    return Content(title=title[:300], entity_type=kind, entity_id=None, link_path="/finance/dsc")


def _register_dsc() -> None:
    from app.events.billing import CreditNoteIssued, InvoiceIssued
    from app.services import files_service
    from app.services.billing import dsc
    from app.services.notifications_service import (
        NOTIFICATION_TYPES,
        NotificationType,
        register_event_source,
        register_notification_type,
    )

    subscribe(InvoiceIssued, _on_doc_issued_dsc)
    subscribe(CreditNoteIssued, _on_doc_issued_dsc)
    subscribe(dsc.DscSignQueued, _on_dsc_queued)
    for owner, plural in (("invoice", "invoices"), ("credit_note", "credit-notes")):
        if files_service.entity_type_for_path(plural) is None:
            files_service.register_access_checker(owner, dsc.file_access, plural=plural)
    if dsc.NOTIFY_KEY not in NOTIFICATION_TYPES:
        register_notification_type(
            dsc.NOTIFY_KEY,
            NotificationType(
                "Invoice waiting for DSC signature", "Finance", "both", False, _dsc_admins, _render_dsc_pending,
                internal_only=True,
            ),
        )
        register_event_source(dsc.DscSigningPending, dsc.NOTIFY_KEY)


try:
    _register_dsc()
except Exception:  # pragma: no cover - never block other domains' registrations
    logger.exception("billing registration _register_dsc failed")
