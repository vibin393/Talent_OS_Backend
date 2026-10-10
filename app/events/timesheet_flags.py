# ruff: noqa: E501
"""Timesheet flag domain events (Epic 63; dispatched after commit). Ids / scalars only.

* ``TimesheetFlagsRaised`` -- a submitted timesheet version has at least one rule flag (Timesheet Agent trigger
  ``flags_raised``, notification ``timesheet_anomaly_flagged``, workflow trigger ``timesheet_flags.flags_raised``).
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetFlagsRaised:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    flag_count: int
    max_severity: str
    timesheet_version: int


__all__ = ["TimesheetFlagsRaised"]
