# ruff: file-ignore[line-too-long]
"""Ledger sync wiring (Epic 71), autodiscovered by ``core.hooks``.

* the Connector Manifests ``zoho_books`` and ``tally_xml`` (accounting);
* the internal outbox types ``credit_note.issued``, ``debit_note.issued``, ``invoice.cancelled``;
* the sync handlers (``accounting``: push_document, pull_payments) and the job listener (dead pushes -> ``failed``);
* the ★ registration ``payment_suggestion -> confirmed`` (human only);
* notification types, the settings hub entry and audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


def _manifests() -> None:
    from app.core.integrations import register_connector
    from app.services.connectors.accounting.manifests import LEDGER_MANIFESTS

    for m in LEDGER_MANIFESTS:
        register_connector(m)


def _outbox() -> None:
    from app.services.ledger_sync import outbox

    outbox.register()


def _handlers() -> None:
    from app.services import integrations_service
    from app.services.ledger_sync import payments, push

    integrations_service.register_sync_handler("accounting", "push_document", push.push_document_handler)
    integrations_service.register_sync_handler("accounting", "pull_payments", payments.pull_payments)


def _star() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("payment_suggestion", ["confirmed"])


def _job_listener() -> None:
    from app.services.connectors.registry import register_job_listener

    async def on_outcome(ctx: Any, db: AsyncSession, job: Any, outcome: str, exc: BaseException | None) -> None:  # ruff: ignore[unused-function-argument]
        if outcome not in ("dead", "permanent", "auth_failed") or job.op != "sync:push_document" or job.category != "accounting":
            return
        payload = dict(job.payload or {})
        if not payload.get("subject_id") or not job.installation_id:
            return
        from sqlalchemy import select

        from app.models.ledger_sync import LedgerSyncRecord
        from app.services.ledger_sync import common

        rec = (
            await db.execute(
                select(LedgerSyncRecord).where(LedgerSyncRecord.installation_id == job.installation_id, LedgerSyncRecord.document_id == uuid.UUID(str(payload["subject_id"])))
            )
        ).scalar_one_or_none()
        if rec is None:
            return
        rec.status = "failed"
        rec.message = common.sanitise(str(exc) if exc else "The push failed after retries") or "The push failed"
        await db.flush()
        common.notify(rec.tenant_id, "ledger_sync_failed", {"event_id": f"ledger_sync_failed:{rec.id}:{job.id}", "record_id": str(rec.id), "document_kind": rec.document_kind, "document_id": str(rec.document_id)})

    register_job_listener(on_outcome)


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def finance(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        from app.services.identity_bridge import users_with_permission

        return {u["id"] for u in await users_with_permission(db, "ledger:sync")}

    async def failed(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title="A document couldn't be pushed to the accounting system",
            body="Open Ledger sync to see the message, fix the mapping and retry.",
            entity_type=None, entity_id=None, link_path="/finance/ledger-sync",
        )  # fmt: skip

    async def waiting(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        n = int(payload.get("count") or 0)
        return ns.Content(
            title=f"{n} payment{'s' if n != 1 else ''} from Zoho Books to review",
            body="Review and record them in Talent OS. Nothing is recorded until you confirm.",
            entity_type=None, entity_id=None, link_path="/finance/ledger-sync?tab=suggestions",
        )  # fmt: skip

    async def missing(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title="A ledger mapping is missing", body="Map the ledger before the next sync.",
            entity_type=None, entity_id=None, link_path="/settings/ledger",
        )  # fmt: skip

    for key, label, channel, render in (
        ("ledger_sync_failed", "Ledger sync failed", "both", failed),
        ("payment_suggestions_waiting", "Payments waiting for review", "both", waiting),
        ("ledger_mapping_missing", "Ledger mapping missing", "in_app", missing),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, ns.NotificationType(label, "Integrations", channel, False, finance, render, internal_only=True))


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "ledger_sync", "Ledger sync",
        "Automatic pushes to the accounting system, payment checks and suggestion expiry.",
        "/api/v1/ledger/settings", "ledger:sync", owner_epic="Epic 71",
    )  # fmt: skip


def _audit() -> None:
    from app.core import audit

    for key, text in (
        ("ledger.mapping_saved", "Ledger mapping saved"),
        ("ledger.record_retried", "A ledger push was retried"),
        ("ledger.record_skipped", "A ledger push was skipped"),
        ("ledger.run_completed", "A ledger sync run completed"),
        ("ledger.run_failed", "A ledger sync run failed"),
        ("ledger.tally_file_downloaded", "A Tally import file was downloaded"),
        ("ledger.suggestion_confirmed", "A payment suggestion was recorded as a payment"),
        ("ledger.suggestion_rejected", "A payment suggestion was rejected"),
        ("ledger.reconciliation_exported", "A ledger reconciliation was exported"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_manifests, _outbox, _handlers, _star, _job_listener, _notifications, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("ledger_sync hooks: {} failed", _fn.__name__)
        raise
