# ruff: file-ignore[line-too-long]
"""Custom report builder & scheduled reports (Epic 47, FR-237..FR-240) -- facade ``reporting.builder``.

Extends the P1 ``services/reporting`` package (a top-layer reader, AD-40). Every report query compiles through
``core.semantic`` with the **viewer's** (or scheduled recipient's) context: there is no SQL text, no expression
language and no owner-elevated run anywhere in this package.

Modules: ``sources_p1p2`` (P1/P2 semantic sources), ``definitions`` (CRUD + versions), ``validate`` (repair check,
viz keys), ``viewer`` (pure viewer adaptation), ``execute`` (viewer-scoped runs, limits), ``runs`` (preview / run /
export / download), ``sharing``, ``pins``, ``schedules``, ``delivery`` (render + deliver), ``repair``,
``governance`` (limits, audit, run log), ``hooks``.
"""

from app.services.reporting.builder.definitions import (
    archive,
    create,
    get,
    list_reports,
    quick_search_definitions,
    search_definitions,
    update,
)
from app.services.reporting.builder.governance import get_settings, list_runs, put_settings
from app.services.reporting.builder.pins import list_pins, order_pins, pin, unpin
from app.services.reporting.builder.repair import validate_all
from app.services.reporting.builder.runs import download, export_report, preview, run_export, run_report
from app.services.reporting.builder.schedules import create as create_schedule
from app.services.reporting.builder.schedules import delete as delete_schedule
from app.services.reporting.builder.schedules import evaluate_due, list_schedules, resume, run_for_recipient
from app.services.reporting.builder.schedules import update as update_schedule
from app.services.reporting.builder.sharing import put_shares

__all__ = [
    "archive",
    "create",
    "create_schedule",
    "delete_schedule",
    "download",
    "evaluate_due",
    "export_report",
    "get",
    "get_settings",
    "list_pins",
    "list_reports",
    "list_runs",
    "list_schedules",
    "order_pins",
    "pin",
    "preview",
    "put_settings",
    "put_shares",
    "quick_search_definitions",
    "resume",
    "run_export",
    "run_for_recipient",
    "run_report",
    "search_definitions",
    "unpin",
    "update",
    "update_schedule",
    "validate_all",
]
