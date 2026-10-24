# ruff: file-ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation, unused-function-argument]
"""Marketplace registrations (Epic 69), autodiscovered:

* the ``installation_grant`` approval subject (P2 engine; human approvers only);
* notification types ``installation_pending_approval``, ``installation_degraded``, ``installation_test_failed``;
* the settings hub entry (category ``integrations``, key ``marketplace``);
* ★ transitions of ``app_installation`` (``active_from_pending``, ``upgrade``, ``resume``);
* the retention binding of ``installation_health_log`` (class ``connector_payloads``);
* health wiring: ``ConnectorSyncFailed`` (>= 3) -> ``marketplace.sync_failed`` task; a job listener for recovery;
* the consented egress-host pin in the connector registry (re-consent keeps the old hosts until upgrade);
* the automated-checks kicker (``marketplace.run_checks``) and the platform catalogue sync at worker start.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.core.hooks import register_post_discovery
from app.events.ecosystem import ConnectorSyncFailed
from app.services.marketplace import approvals, health, notify, reconsent, submissions


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "marketplace", "Integrations",
        "Install and manage connectors and apps, and set who may install what.",
        "/api/v1/marketplace/listings", "integration:view", "Epic 69",
    )  # fmt: skip


def _human_only() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("app_installation", ["active_from_pending", "upgrade", "resume"])


def _retention() -> None:
    from app.models.marketplace import InstallationHealthLog
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    if "installation_health_log" in retention_bindings():
        return

    async def _no_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> None:  # NOSONAR
        return None

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(
                select(InstallationHealthLog.checked_at).where(InstallationHealthLog.id == row_id)
            )
        ).scalar_one_or_none()

    async def _expire(db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete") -> dict[str, int]:
        result = await db.execute(
            delete(InstallationHealthLog).where(InstallationHealthLog.id.in_(list(ids)))
        )
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    register_retention_binding(
        "installation_health_log",
        "connector_payloads",
        subject_resolver=_no_subject,
        anchor_resolver=_anchor,
        expire_fn=_expire,
    )


def _on_sync_failed(e: ConnectorSyncFailed) -> None:
    if e.consecutive_failures < health.DEGRADE_AFTER_FAILURES:
        return
    from app.tasks import marketplace as tasks
    from app.tasks.runtime import enqueue

    enqueue(
        tasks.sync_failed, str(e.tenant_id), str(e.installation_id), int(e.consecutive_failures), e.error_code
    )


async def _job_listener(
    ctx: Any, db: AsyncSession, job: Any, outcome: str, exc: BaseException | None
) -> None:
    if outcome == "succeeded" and getattr(job, "installation_id", None):
        await health.on_job_succeeded(db, job.installation_id)


def _kick_checks(version_id: uuid.UUID) -> None:
    from app.tasks import marketplace as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.run_checks, str(version_id))


def _sync_catalog_at_start() -> None:
    """Worker / beat start: enqueue the manifest -> listing sync (skipped in eager mode: tests sync explicitly)."""
    from app.core.celery import celery_app

    if celery_app.conf.task_always_eager:
        return
    from app.tasks import marketplace as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.sync_platform_catalog)


def _wire() -> None:
    from app.services.connectors import registry as connector_registry

    connector_registry.register_job_listener(_job_listener)
    connector_registry.register_egress_pin(reconsent.consented_hosts)
    submissions.set_checks_kicker(_kick_checks)
    subscribe(ConnectorSyncFailed, _on_sync_failed)


approvals.register()
notify.register()
_settings()
_human_only()
_retention()
_wire()
register_post_discovery(_sync_catalog_at_start)
