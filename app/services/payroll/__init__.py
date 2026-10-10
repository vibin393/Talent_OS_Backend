"""Payroll contract (Epic 28): pay periods. Contract stubs land in Epic 13; Epic 28 fills them."""

from app.services.payroll import service
from app.services.payroll.service import pay_period_for

__all__ = ["pay_period_for", "service"]
