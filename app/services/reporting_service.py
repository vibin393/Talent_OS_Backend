"""``app.services.reporting_service`` -- the reporting domain's public service module (AD-1 naming).

Re-exports the facade of the ``app.services.reporting`` package (dashboards, report lists, exports)."""

from app.services.reporting import *  # ruff: ignore[undefined-local-with-import-star]
from app.services.reporting import ExportResult, register  # ruff: ignore[unused-import]  # noqa: F401
