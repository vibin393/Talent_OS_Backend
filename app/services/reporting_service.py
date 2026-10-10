"""``app.services.reporting_service`` -- the reporting domain's public service module (AD-1 naming).

Re-exports the facade of the ``app.services.reporting`` package (dashboards, report lists, exports)."""

from app.services.reporting import *  # noqa: F403
from app.services.reporting import ExportResult, register  # noqa: F401
