# ruff: file-ignore[line-too-long]
"""HRMS sync wiring (Epic 71), autodiscovered by ``core.hooks``.

* the Connector Manifests ``darwinbox``, ``keka`` (hrms) and ``darwinbox_payroll``, ``keka_payroll`` (payroll);
* the internal outbox types ``placement.joined``, ``placement.changed``, ``payroll_export.finalized``;
* the sync handlers (``hrms``: push_hire / push_change / push_exit / manual_push / pull_status; ``payroll``:
  payroll_push) and the job listener (dead pushes -> record ``failed`` + ``hrms_sync_failed``);
* the ``hrms_transfer`` default notice, notification types, the settings hub entry and audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

NOTICE_TITLE = "Your employment record in our HR system"
NOTICE_BODY = (
    "We record your employment in our HR system for payroll, HR and statutory purposes. The fields shared are your "
    "name, work email, phone, date of joining, designation, department, location, employment type and manager. "
    "Bank, PAN or Aadhaar reference details are shared only when our Finance team turns them on for a recorded "
    "purpose. Employment and tax records are kept as the law requires."
)


def _manifests() -> None:
    from app.core.integrations import register_connector
    from app.services.connectors.hrms.manifests import HRMS_MANIFESTS
    from app.services.connectors.payroll.manifests_p5 import PAYROLL_P5_MANIFESTS

    for m in (*HRMS_MANIFESTS, *PAYROLL_P5_MANIFESTS):
        register_connector(m)


def _outbox() -> None:
    from app.services.hrms_sync import outbox

    outbox.register()


def _handlers() -> None:
    from app.services import integrations_service
    from app.services.hrms_sync import payroll_push, pull, push

    for kind in ("hire", "change", "exit"):
        integrations_service.register_sync_handler("hrms", f"push_{kind}", push.sync_handler(kind))
    integrations_service.register_sync_handler("hrms", "manual_push", push.manual_push)
    integrations_service.register_sync_handler("hrms", "pull_status", pull.pull_status)
    integrations_service.register_sync_handler("payroll", "payroll_push", payroll_push.payroll_push)


def _job_listener() -> None:
    from app.services.connectors.registry import register_job_listener

    async def on_outcome(ctx: Any, db: AsyncSession, job: Any, outcome: str, exc: BaseException | None) -> None:  # ruff: ignore[unused-function-argument]
        if outcome not in ("dead", "permanent", "auth_failed") or not str(job.op).startswith("sync:push_") or job.category != "hrms":
            return
        payload = dict(job.payload or {})
        if payload.get("subject_type") != "placement" or not job.installation_id:
            return
        from app.services.hrms_sync import common
        from app.services.hrms_sync.push import find_record

        rec = await find_record(db, job.installation_id, uuid.UUID(str(payload["subject_id"])))
        if rec is None:
            return
        rec.status = "failed"
        rec.last_message = common.sanitise(str(exc) if exc else "The push failed after retries") or "The push failed"
        await db.flush()
        from app.models.oauth import AppInstallation

        inst = await db.get(AppInstallation, job.installation_id)
        common.notify(rec.tenant_id, "hrms_sync_failed", {
            "event_id": f"hrms_sync_failed:{rec.id}:{job.id}", "record_id": str(rec.id), "placement_id": str(rec.placement_id),
            "user_id": str(inst.accountable_user_id) if inst is not None and inst.accountable_user_id else None,
        })  # fmt: skip

    register_job_listener(on_outcome)


def _notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("hrms_transfer", NOTICE_TITLE, NOTICE_BODY)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        uid = _uuid(payload.get("user_id"))
        if uid:
            return {uid}
        from app.services.identity_bridge import users_with_permission

        return {u["id"] for u in await users_with_permission(db, "hrms:sync")}

    async def failed(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title="A worker couldn't be pushed to the HR system",
            body="Open the HRMS sync queue to see the provider message and retry.",
            entity_type="placement", entity_id=_uuid(payload.get("placement_id")), link_path="/hrms/sync?tab=failed",
        )  # fmt: skip

    async def mismatch(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title=str(payload.get("title") or "HRMS and Talent OS disagree"),
            body="Check the placement and mark the task resolved when both systems agree. Nothing was changed automatically.",
            entity_type="placement", entity_id=_uuid(payload.get("placement_id")), link_path="/hrms/sync?tab=mismatches",
        )  # fmt: skip

    for key, label, channel, recipients, render in (
        ("hrms_sync_failed", "HRMS push failed", "both", to_user, failed),
        ("hrms_mismatch", "HRMS status differs", "in_app", to_user, mismatch),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, ns.NotificationType(label, "Integrations", channel, False, recipients, render, internal_only=True))


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "hrms_sync", "HRMS sync",
        "Which workers are pushed to the HR system, when, and the daily status check.",
        "/api/v1/hrms/settings", "hrms:sync", owner_epic="Epic 71",
    )  # fmt: skip


def _audit() -> None:
    from app.core import audit

    for key, text in (
        ("hrms.mapping_saved", "HRMS field mapping saved"),
        ("hrms.sensitive_mapping_enabled", "A sensitive HRMS field was turned on"),
        ("hrms.sensitive_mapping_disabled", "A sensitive HRMS field was turned off"),
        ("hrms.worker_pushed", "A worker was pushed to the HR system"),
        ("hrms.record_retried", "An HRMS push was retried"),
        ("hrms.record_skipped", "An HRMS push was skipped"),
        ("hrms.include_changed", "HRMS sync inclusion changed for a placement"),
        ("hrms.import_file_generated", "An HRMS import file was generated"),
        ("payroll.provider_status_set", "Payroll provider status recorded"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_manifests, _outbox, _handlers, _job_listener, _notice, _notifications, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("hrms_sync hooks: {} failed", _fn.__name__)
        raise
