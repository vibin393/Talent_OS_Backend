"""Registrations of the report builder (Epic 47; autodiscovered, also called from ``reporting/hooks.py``).

* the P1 / P2 report sources (``sources_p1p2.register``) -- the only place they are registered;
* notification types ``report_delivered`` (link variant), ``report_failed`` (foundation 0.7 keys) and the
  domain-local ``report_needs_repair`` (in-app + email, internal only);
* the settings-hub entry ``report_builder`` (``/reports/custom/settings``);
* the ``files`` access checker of ``report_run`` files (the run's principal only, until expiry);
* the after-commit handler of ``ReportExportQueued`` (enqueues ``reports.run_export``).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["register"]


def _payload_user(*keys: str) -> Any:
    async def resolve(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
        out: set[uuid.UUID] = set()
        for key in keys:
            value = payload.get(key)
            if value:
                try:
                    out.add(uuid.UUID(str(value)))
                except ValueError:
                    continue
        return out

    return resolve


def _renderer(title: str) -> Any:
    async def render(db: AsyncSession, payload: Mapping[str, Any]) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        from app.services import notifications_service as notifications

        return notifications.Content(title=str(payload.get("title") or title), body=payload.get("body"))

    return render


def _register_notifications() -> None:
    from app.services import notifications_service as notifications

    types = {
        "report_delivered": notifications.NotificationType(
            "Report ready to download", "Reports", "in_app", False,
            _payload_user("user_id"), _renderer("Your report is ready"), internal_only=True,
        ),
        "report_failed": notifications.NotificationType(
            "Scheduled report failed or paused", "Reports", "both", False,
            _payload_user("owner_id"), _renderer("A scheduled report failed"), internal_only=True,
        ),
        "report_needs_repair": notifications.NotificationType(
            "Report needs repair", "Reports", "both", False,
            _payload_user("owner_id"), _renderer("A report needs repair"), internal_only=True,
        ),
    }  # fmt: skip
    for key, definition in types.items():
        if key not in notifications.NOTIFICATION_TYPES:
            notifications.register_notification_type(key, definition)


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "reporting",
        "report_builder",
        "Custom reports",
        "Preview / export row caps, query time limit, scheduled-report attachments and link lifetime.",
        "/api/v1/reports/custom/settings",
        "report:share_tenant",
        owner_epic="Epic 47",
    )


def _register_files() -> None:
    from app.services import files_service
    from app.services.reporting.builder import runs

    files_service.register_access_checker("report_run", runs.file_access, plural="report-runs")


def _on_export_queued(event: Any) -> None:
    try:
        from app.tasks import reports as report_tasks
        from app.tasks.runtime import enqueue

        enqueue(report_tasks.run_export, str(event.tenant_id), str(event.run_id))
    except Exception as exc:  # never breaks the committing request
        logger.warning("Could not enqueue a report export ({})", type(exc).__name__)


def _register_handlers() -> None:
    from app.core.events import subscribe
    from app.services.reporting.builder.events import ReportExportQueued

    subscribe(ReportExportQueued, _on_export_queued)


_registered = False


def register() -> None:
    global _registered  # process-wide wiring, idempotent
    if _registered:
        return
    _registered = True
    from app.services.reporting.builder import sources_p1p2

    sources_p1p2.register()
    for step in (_register_notifications, _register_settings, _register_files, _register_handlers):
        try:
            step()
        except Exception as exc:  # pragma: no cover - a registry drift must not break startup
            logger.warning("report builder registration {} failed ({})", step.__name__, exc)


register()
