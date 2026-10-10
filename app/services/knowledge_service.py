# ruff: noqa: E501
"""Facade of the ``knowledge`` domain (Epic 53 contract layer, spec 0.12; filled by Epic 62). Other domains, routers,
tools and agents call these, never the package internals.

* ``search`` -- the frozen contract (``tests/contracts/signatures.json``) behind the core tool ``knowledge.search``.
* ``quick_search`` -- C-16: a quick internal search across every published source (Epic 65 / Copilot).
* ``sources`` -- Knowledge Sources admin; ``qa`` -- Onboarding Q&A (portal, agent-side and coordinator functions);
  ``gaps`` -- the FAQ gap report; ``thread_owner`` -- the onboarding agent's Accountable Owner.
"""

from app.services.knowledge import gaps, qa, sources
from app.services.knowledge.common import CADENCE, HANDOFF_TEXT, HANDOFF_TOPICS, UNAVAILABLE_TEXT
from app.services.knowledge.qa import thread_owner
from app.services.knowledge.service import quick_search, search

__all__ = [
    "CADENCE",
    "HANDOFF_TEXT",
    "HANDOFF_TOPICS",
    "UNAVAILABLE_TEXT",
    "gaps",
    "qa",
    "quick_search",
    "search",
    "sources",
    "thread_owner",
]
