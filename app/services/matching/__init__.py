"""Matching service package (Epic 5, stories 5.4/5.5; AD-8 deterministic scores, the LLM only explains).

Public API: ``service`` (match lists, funnel, recompute, dismiss / restore / add-to-pipeline), ``runs``
(scoring runs, candidate refresh), ``explain``, ``settings`` (tenant weights), ``scoring`` (pure),
``register_pipeline_gateway`` (Epic 6 plugs the pipeline in).
"""

from app.services.matching import (
    explain,
    handlers,
    ports,
    rediscovery,
    runs,
    scoring,
    sections,  # Epic 41 (additive): match list section registry
    service,
    settings,
)
from app.services.matching.ports import PipelineGateway, register_pipeline_gateway

handlers.register()

__all__ = [
    "PipelineGateway",
    "explain",
    "handlers",
    "ports",
    "rediscovery",
    "register_pipeline_gateway",
    "runs",
    "scoring",
    "sections",
    "service",
    "settings",
]
