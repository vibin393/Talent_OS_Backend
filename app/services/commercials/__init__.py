"""Client commercials (Epic 16, FR-88..FR-92): billing profiles, payment terms, contracts (MSA / SOW /
Amendment / Vendor Agreement), SOW milestones, rate cards, rate checks + the rate-exception gate, purchase
orders and the append-only PO consumption ledger.

Public API for other domains (names fixed by their specs; import through ``app.services.commercials_service``):

* contract layer: ``validate_rate``, ``record_po_consumption``, ``po_balance``, ``active_contract_for``,
  ``create_contract_draft``;
* rates: ``require_rate_clearance(ctx, db, req: RateCheckRequest) -> RateCheckResult`` (409
  ``rate_exception_required|pending|rejected``), ``copy_rate_card_template``;
* contracts: ``assert_contract_active``, ``get_contract -> ContractFacts``, ``shares_bgv_outcome``,
  ``transition_contract``;
* billing: ``get_billing_profile -> BillingProfileFacts``, ``assert_invoiceable``, ``due_date``;
* POs: ``validate_po -> PoValidity``, ``default_po -> PoRef | None``, ``check_po_headroom``,
  ``request_po_exception``;
* portal: ``portal.client_po_consumption`` (mounted by the Client Portal epic);
* Epic 45: ``mark_milestone_billable(ctx, db, milestone_id, *, actor) -> MilestoneOut`` (Deliverable acceptance).
"""  # ruff: ignore[line-too-long]

from app.services.commercials import (
    billing_profiles,
    consumption,
    contracts,
    gates,
    milestones,
    payment_terms,
    portal,
    purchase_orders,
    rate_cards,
    rate_checks,
    rate_validation,
    service,
    settings,
)
from app.services.commercials.billing_profiles import assert_invoiceable, get_billing_profile
from app.services.commercials.consumption import request_po_exception
from app.services.commercials.contracts import (
    assert_contract_active,
    get_contract,
    shares_bgv_outcome,
    transition_contract,
)
from app.services.commercials.milestones import mark_milestone_billable
from app.services.commercials.payment_terms import due_date
from app.services.commercials.purchase_orders import check_po_headroom, default_po, validate_po
from app.services.commercials.rate_cards import copy_rate_card_template
from app.services.commercials.rate_checks import require_rate_clearance
from app.services.commercials.service import (
    active_contract_for,
    create_contract_draft,
    po_balance,
    record_po_consumption,
    validate_rate,
)

__all__ = [
    "active_contract_for",
    "assert_contract_active",
    "assert_invoiceable",
    "billing_profiles",
    "check_po_headroom",
    "consumption",
    "contracts",
    "copy_rate_card_template",
    "create_contract_draft",
    "default_po",
    "due_date",
    "gates",
    "get_billing_profile",
    "get_contract",
    "mark_milestone_billable",
    "milestones",
    "payment_terms",
    "po_balance",
    "portal",
    "purchase_orders",
    "rate_cards",
    "rate_checks",
    "rate_validation",
    "record_po_consumption",
    "request_po_exception",
    "require_rate_clearance",
    "service",
    "settings",
    "shares_bgv_outcome",
    "transition_contract",
    "validate_po",
    "validate_rate",
]
