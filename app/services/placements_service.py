"""Facade of the ``placements`` domain (Epic 13 contract layer; filled by Epic 19). Other domains import from here."""

from app.services.placements import rate_segments
from app.services.placements.gates import assert_clear_for_update
from app.services.placements.registry import (
    GateResolver,
    GateState,
    StartGateKindDef,
    VerifierResult,
    offboarding_verifiers,
    placement_filters,
    register_offboarding_verifier,
    register_placement_filter,
    register_start_gate_kind,
    register_worker_360_section,
    start_gate_kinds,
    worker_360_sections,
)
from app.services.placements.service import (
    billing_context,
    create_direct,
    create_from_offer,
    get_placement_summary,
    get_rate_segment,
    link_project,
    pay_context,
    rate_segment_on,
    worker_payment_identity,
)
from app.services.placements.status import transition_placement
from app.services.placements.workers import (
    activate_bank_details,
    confirm_pan,
    ensure_worker_profile,
    record_bank_details,
    record_pan,
    request_bank_change,
    update_profile,
)

__all__ = [
    "GateResolver",
    "GateState",
    "StartGateKindDef",
    "VerifierResult",
    "activate_bank_details",
    "assert_clear_for_update",
    "billing_context",
    "confirm_pan",
    "create_direct",
    "create_from_offer",
    "ensure_worker_profile",
    "get_placement_summary",
    "get_rate_segment",
    "link_project",
    "offboarding_verifiers",
    "pay_context",
    "placement_filters",
    "rate_segment_on",
    "rate_segments",
    "record_bank_details",
    "record_pan",
    "register_offboarding_verifier",
    "register_placement_filter",
    "register_start_gate_kind",
    "register_worker_360_section",
    "request_bank_change",
    "start_gate_kinds",
    "transition_placement",
    "update_profile",
    "worker_360_sections",
    "worker_payment_identity",
]
