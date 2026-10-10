"""Facade of the ``time`` domain (Epic 13 contract layer; filled by Epic 25). Other domains import from here.

* Contract: ``billing_inputs``, ``pay_inputs`` (R10 fields).
* Read functions: ``timesheet_state_for`` (Epic 26), ``monitor`` (Epic 34),
  ``unapproved_timesheets`` (Epic 28),
  ``link_summary`` (Epic 18).
* Registries: ``register_prefill_source`` (Epic 26), ``calendar`` (working-day maths, AD-20).
* Epic 63 (additive): ``remind`` (the one reminder path, 4 h gap), ``config_version`` and
  ``holidays_for`` (readers for the Timesheet Agent's nudges and anomaly flags).
"""

from app.services.time import calendar
from app.services.time.configs import config_version
from app.services.time.holidays import holidays_for
from app.services.time.monitoring import remind
from app.services.time.prefill import register_prefill_source
from app.services.time.service import (
    billing_inputs,
    link_summary,
    monitor,
    pay_inputs,
    timesheet_state_for,
    unapproved_timesheets,
)

__all__ = [
    "billing_inputs",
    "calendar",
    "config_version",
    "holidays_for",
    "link_summary",
    "monitor",
    "pay_inputs",
    "register_prefill_source",
    "remind",
    "timesheet_state_for",
    "unapproved_timesheets",
]
