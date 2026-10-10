"""Facade of the ``crm`` domain (Epic 15). Routers and other callers import from here."""

from app.services.crm import conversion, dashboard, duplicates, imports, leads, opportunities, settings, won
from app.services.crm.conversion import convert_lead, related_activity_refs
from app.services.crm.duplicates import find_duplicates
from app.services.crm.leads import mark_contacted, transition_lead
from app.services.crm.opportunities import transition_opportunity
from app.services.crm.won import record_link, win_opportunity, won_preview

__all__ = [
    "conversion",
    "convert_lead",
    "dashboard",
    "duplicates",
    "find_duplicates",
    "imports",
    "leads",
    "mark_contacted",
    "opportunities",
    "record_link",
    "related_activity_refs",
    "settings",
    "transition_lead",
    "transition_opportunity",
    "win_opportunity",
    "won",
    "won_preview",
]
