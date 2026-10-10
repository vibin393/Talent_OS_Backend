"""Compatibility shim (Epic 13): the business-hours calculator moved to ``app.core.business_hours``."""

from app.core.business_hours import *  # noqa: F403
from app.core.business_hours import (
    WEEKDAYS,
    Calendar,
    add_business_hours,
    add_calendar_hours,
    elapsed_business_hours,
    elapsed_calendar_hours,
    tenant_calendar,
    validate_work_week,
)

__all__ = [
    "WEEKDAYS",
    "Calendar",
    "add_business_hours",
    "add_calendar_hours",
    "elapsed_business_hours",
    "elapsed_calendar_hours",
    "tenant_calendar",
    "validate_work_week",
]
