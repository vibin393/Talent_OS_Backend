"""``app.services.pipeline_service`` -- the pipeline domain's public service module (AD-1 naming).

Re-exports the facade of the ``app.services.pipeline`` package so other domains can resolve it either way
(Epic 10 resolves ``app.services.pipeline_service`` lazily): ``transition_application``, ``TRANSITIONS``,
``add_candidate``, ``demand_progress_counts``, ``client_submission_summary``, ``workflow_snapshot``,
``HUMAN_ONLY`` ...
"""

from app.services.pipeline import *  # noqa: F403
from app.services.pipeline import HUMAN_ONLY, TRANSITIONS, PipelineGateway, register  # noqa: F401
