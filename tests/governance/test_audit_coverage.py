"""FR-183 / AD-5 audit coverage (Epic 35 story 35.5, spec A6).

1. Every Phase 2 model inherits ``AuditedMixin`` unless it is in ``AUDIT_EXEMPT`` (with a justification) or
   in ``PENDING_AUDIT`` (a gap its owner epic still has to close). Pending entries are skips; an entry
   that is audited by now fails so it is removed from the list.
2. The explicit action catalogue: each key in ``REQUIRED_ACTIONS`` is registered by its owner through
   ``audit.register_audit_action`` (after ``hooks`` autodiscovery), or listed in ``PENDING_ACTIONS``.
"""

import importlib
import pkgutil

import pytest

import app.models
from app.core import audit
from app.core.hooks import autodiscover
from app.core.models import Base

# Model modules introduced by Phase 2 (epics 13-35). Epic 52 adds the Phase 3 modules (``P3_MODEL_MODULES``);
# Phase 3 tables in the shared P2 modules (billing / receivables / legal_entities / sso) are covered above.
P2_MODEL_MODULES: tuple[str, ...] = (
    "bgv", "billing", "client_portal", "commercials", "compliance", "connectors", "crm", "documents",
    "expenses", "external_auth", "golive", "legal_entities", "payroll", "placements", "placements_exit",
    "portal_notifications", "postings", "receivables", "sso", "time", "vendor_statements", "vendors",
)  # fmt: skip

# Epic 52 (A5, FR-261): Phase 3 model modules (epics 36-51).
P3_MODEL_MODULES: tuple[str, ...] = (
    "skills_graph", "talent_pools", "campaigns", "assessments", "mobility", "referrals", "campus", "workforce",
    "projects", "immigration", "report_builder", "analytics", "fairness", "p3_hubs",
)  # fmt: skip

_CHILD = "child / line rows: changes are audited on the parent document"
_HISTORY = "append-only history or event log (itself an audit trail)"
_DERIVED = "high-volume derived / projection rows rebuilt from audited sources"
_LOOKUP = "token lookup or session-state row (security plumbing, no business change)"
_JOB = "job / run bookkeeping; the run's outcome is recorded explicitly with audit.record"
_ALERT = "dedupe marker for sent alerts / reminders (no business change)"
_EXPLICIT = "immutable financial row whose creation is recorded explicitly (audit.record) by its service"

AUDIT_EXEMPT: dict[str, str] = {
    # billing (Epic 29)
    "invoice_lines": _CHILD, "invoice_tax_lines": _CHILD, "credit_note_lines": _CHILD,
    "credit_note_tax_lines": _CHILD, "debit_note_lines": _CHILD, "debit_note_tax_lines": _CHILD,
    "invoice_run_exceptions": _JOB, "einvoice_imports": _JOB,
    # bgv (Epic 20)
    "bgv_case_events": _HISTORY, "bgv_evidence": "evidence files; downloads audited as document.downloaded",
    "reference_responses": "secure-link responses captured once, immutable (audited on the reference check)",
    # client portal / external auth (Epics 14, 18)
    "client_portal_downloads": _HISTORY, "org_grants": _DERIVED,
    # commercials (Epic 16)
    "billing_profile_contacts": _CHILD, "rate_card_lines": _CHILD, "contract_reminders": _ALERT,
    "po_alerts": _ALERT, "po_consumptions": _DERIVED, "rate_checks": _DERIVED,
    # compliance (Epic 21)
    "compliance_evaluation_runs": _JOB, "compliance_recompute_runs": _JOB, "credential_alerts": _ALERT,
    # connectors (Epic 22)
    "connector_hook_lookups": _LOOKUP, "connector_inbound_events": _HISTORY, "connector_jobs": _JOB,
    # crm (Epic 15)
    "crm_record_links": _DERIVED, "crm_status_history": _HISTORY,
    # documents / eSign (Epic 22)
    "document_versions": "immutable file versions of an audited document",
    "esign_certificate_lookups": _LOOKUP, "signature_events": _HISTORY, "signer_otps": _LOOKUP,
    "signatures": "immutable signature evidence; esign.signed is recorded explicitly",
    "signers": _CHILD,
    # expenses (Epic 27)
    "expense_lines": _CHILD, "expense_line_decisions": _HISTORY, "expense_policy_categories": _CHILD,
    # legal entities (Epic 14)
    "document_series_counters": "counter row bumped under lock; numbering.assigned is recorded explicitly",
    # payroll (Epic 28)
    "payroll_export_items": _CHILD, "payroll_export_rows": _CHILD, "payroll_export_versions": _HISTORY,
    "pay_periods": _DERIVED,
    # placements / exit (Epics 19, 24, 31)
    "placement_status_history": _HISTORY, "placement_touchpoints": _HISTORY, "start_gates": _DERIVED,
    "bench_match_notices": _ALERT, "bench_match_review_completions": _HISTORY,
    "bench_match_reviews": _DERIVED,
    "offboarding_feedback": "free-text feedback captured once (audited via the offboarding case)",
    "placement_end_alerts": _ALERT, "placement_end_date_history": _HISTORY,
    # postings (Epic 16 ledger)
    "billing_postings": _DERIVED, "payable_postings": _DERIVED,
    # receivables (Epic 30)
    "allocations": _EXPLICIT, "allocation_reversals": _EXPLICIT, "client_credits": _DERIVED,
    "invoice_balances": _DERIVED, "bank_statement_rows": _JOB, "tds_deductions": _EXPLICIT,
    "tds_deduction_reversals": _EXPLICIT, "write_offs": _EXPLICIT, "write_off_reversals": _EXPLICIT,
    "accounting_export_items": _CHILD, "reconciliation_runs": _JOB, "reconciliation_findings": _JOB,
    "period_close_events": _HISTORY,
    # sso (Epic 14)
    "sso_login_states": _LOOKUP,
    # time / leave (Epics 25, 26)
    "timesheet_entries": _CHILD, "timesheet_adjustment_entries": _CHILD, "timesheet_reminders_sent": _ALERT,
    "rate_adjustments": _DERIVED, "leave_balances": _DERIVED, "leave_balance_entries": _HISTORY,
    "holidays": _CHILD, "worker_optional_holidays": _CHILD,
    # vendors (Epic 17)
    "vendor_submission_events": _HISTORY, "vendor_payable_statements": _DERIVED,
    "vendor_statement_lines": _CHILD,
    # ---------------- Epic 52: Phase 3 (epics 36-51)
    # billing / receivables / legal entities carry-overs (Epic 51)
    "dsc_sign_requests": "DSC signing job bookkeeping; signing outcomes are recorded explicitly (audit.record)",
    "fx_feed_runs": _JOB,
    "tds_recon_runs": "26AS run bookkeeping; run creation and match decisions are recorded explicitly",
    "tds_statement_lines": "parsed 26AS statement lines (imported once, immutable)",
    "tds_recon_matches": "match proposals; confirm / reject recorded explicitly (tds_recon_match.*)",
    # analytics (Epic 48)
    "ml_feature_specs": "platform feature catalogue rows synced from code", "ml_training_sets": _JOB,
    "ml_predictions": _DERIVED,
    # assessments (Epic 40)
    "attempts": "candidate attempt state (high-volume autosave); decisions audited on the invitation",
    "attempt_answers": _CHILD, "proctoring_signals": _HISTORY, "proctoring_annotations": _CHILD,
    "review_scores": _CHILD, "submission_assessment_shares": _CHILD,
    "assessment_item_imports": _JOB, "assessment_provider_tests": "cached provider test catalogue (derived)",
    # campaigns (Epic 39)
    "audience_list_entries": _CHILD, "audience_snapshots": _DERIVED, "audience_members": _DERIVED,
    "campaign_messages": _HISTORY, "engagement_events": _HISTORY, "campaign_attributions": _DERIVED,
    "sequence_steps": _CHILD, "marketing_trigger_caps": _ALERT,
    # fairness (Epic 49) -- protected data never enters the generic audit log (AD-47); changes recorded explicitly
    "aedt_alternative_requests": "requests recorded explicitly (fairness.alternative_*)",
    "demographic_category_sets": "versioned; changes recorded explicitly (fairness.category_set_changed)",
    "demographic_dimensions": _CHILD,
    "demographic_self_ids": "protected self-ID answers: never in the audit log (AD-47); self_id.* recorded explicitly",
    "self_id_apply_uses": _LOOKUP, "fairness_runs": _JOB,
    # immigration (Epic 46)
    "visa_type_templates": "platform seed rows (no tenant change)", "visa_type_versions": _HISTORY,
    "case_events": _HISTORY,
    # mobility / projects / reports / skills / pools
    "internal_openings": _DERIVED, "deliverable_events": _HISTORY, "project_burn": _DERIVED,
    "report_runs": _JOB, "skill_edges": "platform graph seed rows (no tenant change)",
    "talent_pool_members": _DERIVED,
}  # fmt: skip

# Settings-type tables that change by admin action and are not audited yet (AD-5 gap; owner epic to fix).
PENDING_AUDIT: dict[str, str] = {
    "crm_settings": "Epic 15 (crm)",
    "payroll_settings": "Epic 28 (payroll)",
    "pay_calendars": "Epic 28 (payroll)",
    "payroll_export_mappings": "Epic 28 (payroll)",
    "placement_exit_settings": "Epic 31 (placement exit)",
}

REQUIRED_ACTIONS: tuple[str, ...] = (
    "identity.unmask", "document.downloaded", "esign.sent", "esign.signed", "esign.completed",
    "numbering.assigned", "period.closed", "period.reopened", "payroll_export.generated",
    "payroll_export.finalized", "retention.expired", "escalation.fired", "golive.applied",
)  # fmt: skip
PENDING_ACTIONS: dict[str, str] = {
    "identity.unmask": "Epic 13 foundation (core.encrypted.unmask records identity_unmasked)",
    "document.downloaded": "Epic 22 (documents)",
    "esign.sent": "Epic 22 (documents)",
    "esign.signed": "Epic 22 (documents)",
    "esign.completed": "Epic 22 (documents)",
    "numbering.assigned": "Epic 14 / 29 (legal_entities numbering)",
    "period.closed": "Epic 30 (receivables period close)",
    "period.reopened": "Epic 30 (receivables period close)",
    "payroll_export.generated": "Epic 28 (payroll)",
    "payroll_export.finalized": "Epic 28 (payroll)",
    "escalation.fired": "Epic 32 (sla escalations)",
}


def _p2_models() -> dict[str, type]:
    for info in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{info.name}")
    out: dict[str, type] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        module = cls.__module__.rsplit(".", 1)[-1]
        if cls.__module__.startswith("app.models.") and module in (*P2_MODEL_MODULES, *P3_MODEL_MODULES):
            out[getattr(cls, "__tablename__", cls.__name__)] = cls
    return out


MODELS = _p2_models()


def test_exempt_entries_are_justified() -> None:
    assert all(reason.strip() for reason in AUDIT_EXEMPT.values())
    assert not set(AUDIT_EXEMPT) & set(PENDING_AUDIT)


def test_every_phase2_model_is_audited_or_exempt() -> None:
    missing = sorted(
        t for t, cls in MODELS.items()
        if not getattr(cls, "__audited__", False) and t not in AUDIT_EXEMPT and t not in PENDING_AUDIT
    )  # fmt: skip
    assert missing == [], (
        f"Phase 2 models without AuditedMixin (add the mixin or an AUDIT_EXEMPT reason): {missing}"
    )


def test_exempt_list_has_no_stale_entries() -> None:
    audited = sorted(t for t in AUDIT_EXEMPT if t in MODELS and getattr(MODELS[t], "__audited__", False))
    assert audited == [], f"now audited, remove from AUDIT_EXEMPT: {audited}"


@pytest.mark.parametrize("table", sorted(PENDING_AUDIT))
def test_pending_audit(table: str) -> None:
    cls = MODELS.get(table)
    if cls is not None and getattr(cls, "__audited__", False):
        pytest.fail(f"{table} is audited now -- remove it from PENDING_AUDIT")
    pytest.skip(f"{table} not audited yet (owner: {PENDING_AUDIT[table]})")


@pytest.mark.parametrize("key", REQUIRED_ACTIONS)
def test_audit_action_catalogue(key: str) -> None:
    autodiscover()
    registered = key in audit.audit_actions()
    if key in PENDING_ACTIONS:
        if registered:
            pytest.fail(f"{key} is registered now -- remove it from PENDING_ACTIONS")
        pytest.skip(f"{key} not registered yet (owner: {PENDING_ACTIONS[key]})")
    assert registered, f"audit action {key} must be registered with audit.register_audit_action"
    assert audit.audit_actions()[key].strip()


def test_actor_resolvers_for_external_principals() -> None:
    import app.services.audit_timeline_service  # noqa: F401 - registers fallback resolvers

    for kind in ("user", "client_user", "vendor_user"):
        assert audit.actor_resolver(kind) is not None, kind
