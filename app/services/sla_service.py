"""Facade of the generic ``sla`` domain (Epic 13, AD-31)."""

from app.services.sla.registry import SlaMilestoneDef, milestones, register_sla_milestone
from app.services.sla.service import cancel_clock, clocks_for, compute_status, start_clock, stop_clock

__all__ = [
    "SlaMilestoneDef",
    "cancel_clock",
    "clocks_for",
    "compute_status",
    "milestones",
    "register_sla_milestone",
    "start_clock",
    "stop_clock",
]
