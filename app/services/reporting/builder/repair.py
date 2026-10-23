# ruff: file-ignore[line-too-long]
"""Needs-repair bookkeeping (Epic 47 story 47.5, FR-240).

* ``flag(db, report, issues)`` -- ``status='needs_repair'`` + ``repair_issues``; active schedules pause with
  ``paused_reason='needs_repair'``; the owner is notified once per broken version (``report_needs_repair``).
* ``validate_all(db)`` -- startup / nightly (``reports.validate_definitions``): every live definition is checked
  against the registry; healthy again -> the flag clears (paused schedules stay paused until resumed).
* Saving a fixed definition clears the flag (``definitions.update``).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.report_builder import ReportDefinition, ReportSchedule
from app.services.reporting.builder.common import now
from app.services.reporting.builder.validate import repair_issues

__all__ = ["clear", "flag", "validate_all"]


async def _notify_owner(db: AsyncSession, report: ReportDefinition, issues: list[dict[str, Any]]) -> None:
    from app.services import notifications_service as notifications

    fields = sorted({str(i.get("field")) for i in issues if i.get("field")})
    body = "Fields that no longer exist: " + ", ".join(fields) if fields else "Its source changed."
    try:
        await notifications.deliver(
            db,
            "report_needs_repair",
            [report.owner_id],
            notifications.Content(
                title=f"Report '{report.name}' needs repair",
                body=body + " Its schedules are paused until you fix and save it.",
                entity_type="report_definition",
                entity_id=report.id,
                link_path=f"/reports/custom/{report.id}",
            ),
            event_id=f"report_needs_repair:{report.id}:{report.definition_version}",
        )
    except Exception:  # a notification never blocks the repair flag
        logger.exception("Could not notify the owner of report {}", report.id)


async def flag(db: AsyncSession, report: ReportDefinition, issues: list[dict[str, Any]]) -> None:
    changed = report.status != "needs_repair" or report.repair_issues != issues
    report.status = "needs_repair"
    report.repair_issues = issues
    paused = (
        (
            await db.execute(
                update(ReportSchedule)
                .where(ReportSchedule.report_id == report.id, ReportSchedule.status == "active")
                .values(status="paused", paused_reason="needs_repair")
                .returning(ReportSchedule.id)
            )
        )
        .scalars()
        .all()
    )
    if changed or report.repair_notified_at is None:
        report.repair_notified_at = now()
        await _notify_owner(db, report, issues)
        audit.record(
            "report.needs_repair",
            "report_definition",
            report.id,
            session=db,
            report_id=str(report.id),
            version=report.definition_version,
            issues=issues,
            paused_schedules=[str(s) for s in paused],
        )
    await db.flush()


def clear(report: ReportDefinition) -> None:
    report.status = "ok"
    report.repair_issues = []
    report.repair_notified_at = None


async def validate_all(db: AsyncSession) -> dict[str, int]:
    """Check every live definition of the session's tenant; returns ``{"checked", "broken", "healed"}``."""
    rows = (
        (
            await db.execute(
                select(ReportDefinition)
                .where(ReportDefinition.archived_at.is_(None))
                .order_by(ReportDefinition.id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    broken = healed = 0
    for report in rows:
        issues = repair_issues(report.definition)
        if issues:
            broken += 1
            await flag(db, report, issues)
        elif report.status == "needs_repair":
            healed += 1
            clear(report)
    await db.flush()
    return {"checked": len(rows), "broken": broken, "healed": healed}


async def report_ids_needing_repair(db: AsyncSession) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(select(ReportDefinition.id).where(ReportDefinition.status == "needs_repair"))
        ).scalars()
    )
