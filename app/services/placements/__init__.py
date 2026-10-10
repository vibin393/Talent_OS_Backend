"""Placements domain (Epic 19): the hub record (AD-25). Contract stubs landed in Epic 13; Epic 19 fills them.

Modules: ``service`` (contract), ``creation``, ``status``, ``gates``, ``joining``, ``prejoining``, ``dropout``,
``margin``, ``rate_segments``, ``workers``, ``lists``, ``portal``, ``merge_source``, ``registry``, ``events``.
"""

from app.services.placements import registry, service
from app.services.placements.service import (
    billing_context,
    create_direct,
    create_from_offer,
    get_placement_summary,
    get_rate_segment,
    pay_context,
    rate_segment_on,
    worker_payment_identity,
)

__all__ = [
    "billing_context",
    "create_direct",
    "create_from_offer",
    "get_placement_summary",
    "get_rate_segment",
    "pay_context",
    "rate_segment_on",
    "registry",
    "service",
    "worker_payment_identity",
]
