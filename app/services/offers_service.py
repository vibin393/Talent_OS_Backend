"""``app.services.offers_service`` -- the offers domain's public service module (AD-1 naming).

Re-exports the facade of the ``app.services.offers`` package: ``transition_offer``, ``TRANSITIONS``,
``create_offer``, ``release_offer``, ``CandidateResponder``, ``ensure_default_templates``, ``HUMAN_ONLY`` ...
"""

from app.services.offers import *  # ruff: ignore[undefined-local-with-import-star]
from app.services.offers import HUMAN_ONLY, TRANSITIONS, register  # ruff: ignore[unused-import]
