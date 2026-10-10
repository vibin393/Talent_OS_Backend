"""Registrations of the ``segments`` domain (autodiscovered by ``core.hooks``), Epic 38 A3.

Adds ``days_since_last_placement`` (``candidates.candidate``) and ``offboarding.reason``
(``placements.worker``) to the base semantic sources (``extend_semantic_source``, decision 11). Segments have
no notification types of their own: repair notices go to Pool owners (``talent_pools`` hooks).
"""

from loguru import logger

try:
    from app.services.segments import semantic_fields

    semantic_fields.register()
except Exception:  # pragma: no cover - never block start-up on an optional registry
    logger.exception("segments: semantic field registration failed")
