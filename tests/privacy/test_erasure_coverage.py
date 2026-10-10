"""A3.4 coverage guard (FR-68): every tenant table that can hold a candidate's personal data must be claimed
by a registered erasure handler.

A table is *candidate-bearing* when it has a ``candidate_id`` column, a foreign key to ``candidates``, a
polymorphic ``(entity|subject|owner_entity)_type/_id`` pair, or its model declares ``__candidate_pii__ = True``.

* ``NON_PERSONAL_TABLES`` -- exceptions, each justified.
* ``PENDING_HANDLER_TABLES`` -- tables of epics being built in parallel that must register their handler
  (see the TODO list in ``app/services/privacy/registry.py``). Any OTHER new candidate-bearing table fails this
  test; an allow-listed table that gains a handler must be removed from the list (second test).
"""

from typing import Any

from sqlalchemy import Table

from app.core.models import Base
from app.core.tenancy import requires_rls
from app.services.privacy import registry

NON_PERSONAL_TABLES: dict[str, str] = {
    # Epic 66 (D-11, AD-79): outbox rows are thin by construction (ids, types, statuses; PII denylist enforced at
    # startup) and purged after 30 days; exposures are ids only and must survive the erasure so ``subject.erased``
    # reaches every Installation that received the subject.
    "outbox_events": "ids, event types and non-personal attributes only (PII denylist); 30-day retention",
    "subject_exposures": "ids only; read by the erasure fan-out (subject.erased, D-11) after the erasure",
    "erasure_runs": "ids, statuses and handler counts only -- proof that an erasure ran",
    "retention_run_items": "ids, last-activity timestamps and statuses only",
    "approval_requests": "approvals of demands / offers / margins (subject is never a candidate)",
    "workflow_executions": "rule execution bookkeeping (entity ids, outcome codes)",
    # Epic 13 (Phase 2 foundation)
    "sla_clocks": "SLA clock bookkeeping (subject ids, timestamps, outcome codes)",
    # Epic 19 (placements): financial-traceability facts and gate bookkeeping; the worker's personal data is
    # claimed by ``placements.worker`` (placements, history, touchpoints, overrides, profiles, bank accounts).
    "rate_segments": "rates / dates of a placement (inherited org ids only), kept for financial traceability",
    # Epic 47 (report builder): definitions name registered semantic keys only; runs hold counts / statuses; the
    # generated files (which can contain personal data) expire after ``link_ttl_days`` (``reports.purge_files``).
    "report_definitions": "report definitions (semantic keys, viz) -- no subject data",
    "report_definition_versions": "append-only definition history -- no subject data",
    "report_shares": "team / role / tenant share targets",
    "report_pins": "a user's dashboard pins",
    "report_schedules": "schedule recurrence and internal recipient user ids",
    "report_runs": "run log (counts, statuses, masked field keys); files purged after link_ttl_days",
    "report_builder_settings": "per-tenant caps",
    # Epic 52 (Phase 3 close): justified non-personal Phase 3 tables.
    "fairness_runs": "aggregate-only snapshots (suppressed cells, no subject ids); kept on self-ID erasure",
    "tds_recon_matches": "26AS tax lines matched to TDS deductions -- deductor / invoice ids, no person",
    # Epic 54 (Copilot): ratings + the user's own optional comment (shown to AI administrators, the UI says so) and
    # tenant caps; conversations and messages are claimed by ``copilot.messages``.
    "copilot_feedback": "a user's rating of a Copilot answer (run id, rating, own comment) -- no candidate data",
    "copilot_settings": "per-tenant Copilot retention / rate-limit caps",
}

# Tables of epics being built concurrently (owner, expected handler). Remove an entry once its handler exists.
PENDING_HANDLER_TABLES: dict[str, str] = {
    # Epic 13 contract layer: the owning epics register the handlers (AD-34 retention classes apply).
    # Epic 36 hub tables: assessment_invitations is claimed by Epic 40 (services/assessments_erasure.py).
}

POLYMORPHIC_ID_COLUMNS = ("entity_id", "subject_id", "owner_entity_id")


def _is_candidate_bearing(table: Table) -> bool:
    names = set(table.c.keys())
    if "candidate_id" in names:
        return True
    if any(fk.column.table.name == "candidates" and fk.parent.name != "tenant_id" for fk in table.foreign_keys):
        return True
    if any(col in names and col.replace("_id", "_type") in names for col in POLYMORPHIC_ID_COLUMNS):
        return True
    return _model_flag(table)


def _model_flag(table: Table) -> bool:
    for mapper in Base.registry.mappers:
        if mapper.local_table is table:
            return bool(getattr(mapper.class_, "__candidate_pii__", False))
    return False


def _candidate_tables() -> dict[str, Any]:
    return {
        t.name: t
        for t in Base.metadata.tables.values()
        if (requires_rls(t.name) or t.name == "audit_logs") and _is_candidate_bearing(t) and t.name != "candidates"
    } | {"candidates": Base.metadata.tables["candidates"]}


def test_every_candidate_bearing_table_is_claimed_by_a_handler() -> None:
    claimed = registry.claimed_tables()
    missing = sorted(
        name
        for name in _candidate_tables()
        if name not in claimed and name not in NON_PERSONAL_TABLES and name not in PENDING_HANDLER_TABLES
    )
    assert missing == [], (
        "candidate-bearing tables without an erasure handler (register one in app/services/<domain>_erasure.py "
        f"or justify it in NON_PERSONAL_TABLES): {missing}"
    )


def test_allowlists_stay_honest() -> None:
    claimed = registry.claimed_tables()
    now_claimed = sorted(t for t in PENDING_HANDLER_TABLES if t in claimed)
    assert now_claimed == [], f"remove from PENDING_HANDLER_TABLES (handler exists now): {now_claimed}"
    overlap = sorted(set(NON_PERSONAL_TABLES) & set(claimed))
    assert overlap == [], f"tables both claimed and listed as non-personal: {overlap}"


def test_claimed_tables_exist_and_handlers_are_ordered() -> None:
    handlers = registry.handlers()
    names = [h.name for h in handlers]
    assert names[-1] == "audit.shred", "audit shred must always run last"
    assert [h.order for h in handlers] == sorted(h.order for h in handlers)
    for h in handlers:
        for table in h.tables:
            assert table in Base.metadata.tables, f"{h.name} claims unknown table {table}"
    assert len(set(names)) == len(names)
    expected = {"candidates.profile", "candidates.merges", "files.candidate", "privacy.consents", "ai.actions"}
    assert expected <= set(names)


def test_new_candidate_table_without_handler_is_detected() -> None:
    """Matrix #9: a table with ``candidate_id`` and no handler fails the guard."""
    from sqlalchemy import Column, MetaData, Uuid

    rogue = Table("rogue_candidate_notes", MetaData(), Column("tenant_id", Uuid()), Column("candidate_id", Uuid()))
    assert _is_candidate_bearing(rogue)
    assert "rogue_candidate_notes" not in registry.claimed_tables()


def test_fairness_self_id_tables_are_claimed() -> None:
    """Epic 49 (NFR-38): the isolated self-ID store and its satellites are erased with the Candidate."""
    claimed = registry.claimed_tables()
    assert {"demographic_self_ids", "aedt_alternative_requests", "self_id_apply_uses"} <= set(claimed)
    assert "demographic_self_ids" in Base.metadata.tables


# ---------------------------------------------------------------- Epic 52 (A1, FR-256 / FR-258): Phase 3
# Phase 3 model modules: detection widens to Client Users, Contacts, referrers and polymorphic subject pairs.
P3_MODEL_MODULES = (
    "app.models.skills_graph", "app.models.talent_pools", "app.models.campaigns", "app.models.assessments",
    "app.models.mobility", "app.models.referrals", "app.models.campus", "app.models.workforce",
    "app.models.projects", "app.models.immigration", "app.models.report_builder", "app.models.analytics",
    "app.models.fairness", "app.models.p3_hubs",
)  # fmt: skip
# Phase 3 tables living in shared (P2) model modules.
P3_TABLES_IN_SHARED_MODULES = ("client_user_identities", "client_sso_exemptions")
P3_PERSONAL_COLUMNS = ("candidate_id", "contact_id", "client_user_id", "referrer_user_id")

# Owner epic -> tables its erasure handler must claim (spec A1).
P3_EXPECTED_HANDLERS: dict[str, tuple[str, ...]] = {
    "Epic 37": ("candidate_skill_evidence",),
    "Epic 38": ("talent_pool_members",),
    "Epic 39": ("audience_members", "enrollments", "campaign_messages", "engagement_events"),
    "Epic 40": ("attempts", "attempt_answers", "proctoring_signals", "review_scores", "assessment_invitations"),
    "Epic 41": ("mobility_applications",),
    "Epic 42": ("referrals", "referral_rewards"),
    "Epic 43": ("registrations", "event_checkins", "queue_entries"),
    "Epic 46": ("immigration_cases", "case_document_requests", "permits"),
    "Epic 48": ("ml_predictions", "ml_training_sets"),
    "Epic 49": ("demographic_self_ids",),
    "Epic 50": ("client_user_identities",),
}

# Every personal Phase 3 table -> retention class key (bound with ``register_retention_binding``) or
# ``"subject"`` (follows the subject's inactive-candidate retention and erasure through the owner's handler).
P3_RETENTION: dict[str, str] = {
    "candidate_skill_evidence": "subject",
    "skill_suggestions": "subject",
    "talent_pool_members": "subject",
    "audience_list_entries": "subject",
    "audience_members": "subject",
    "campaign_attributions": "subject",
    "campaign_messages": "marketing_logs",
    "engagement_events": "marketing_logs",
    "enrollments": "subject",
    "assessment_invitations": "subject",
    "attempts": "subject",
    "attempt_answers": "subject",
    "proctoring_signals": "subject",
    "proctoring_annotations": "subject",
    "review_scores": "subject",
    "submission_assessment_shares": "subject",
    "mobility_employee_links": "subject",
    "mobility_profiles": "subject",
    "mobility_applications": "subject",
    "internal_transfers": "subject",
    "referral_share_links": "subject",
    "referrals": "subject",
    # Paid rewards keep their amounts under finance retention inside the ``referrals.records`` handler (no
    # table binding exists; deviation from the spec's ``financial`` entry, recorded in the Epic 52 notes).
    "referral_rewards": "subject",
    "registrations": "subject",
    "registration_invites": "subject",
    "event_checkins": "subject",
    "queue_entries": "subject",
    "batch_slots": "subject",
    "institution_contacts": "subject",
    "workforce_plans": "subject",
    "project_assignments": "subject",
    "immigration_cases": "immigration",
    "case_events": "subject",
    "case_dependants": "subject",
    "case_document_requests": "subject",
    "permits": "subject",
    "ml_predictions": "subject",
    "ml_training_sets": "subject",
    "demographic_self_ids": "subject",
    "self_id_apply_uses": "subject",
    "aedt_alternative_requests": "subject",
    "client_user_identities": "subject",
    "client_sso_exemptions": "subject",
}


def _p3_tables() -> dict[str, Table]:
    out: dict[str, Table] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if not isinstance(table, Table):
            continue
        if mapper.class_.__module__ in P3_MODEL_MODULES or table.name in P3_TABLES_IN_SHARED_MODULES:
            out[table.name] = table
    return out


def _is_p3_personal(table: Table) -> bool:
    names = set(table.c.keys())
    if any(col in names for col in P3_PERSONAL_COLUMNS):
        return True
    if "subject_id" in names and ("subject_kind" in names or "subject_type" in names):
        return True
    return _is_candidate_bearing(table)


def _p3_personal_tables() -> set[str]:
    claimed = registry.claimed_tables()
    return {
        name
        for name, table in _p3_tables().items()
        if requires_rls(name) and (name in claimed or _is_p3_personal(table)) and name not in NON_PERSONAL_TABLES
    }


def test_p3_personal_tables_are_claimed() -> None:
    claimed = registry.claimed_tables()
    missing = sorted(name for name in _p3_personal_tables() if name not in claimed and name not in PENDING_HANDLER_TABLES)
    assert missing == [], f"Phase 3 personal tables without an erasure handler: {missing}"


def test_p3_expected_owner_handlers() -> None:
    claimed = registry.claimed_tables()
    missing = {
        owner: [t for t in tables if t not in claimed] for owner, tables in P3_EXPECTED_HANDLERS.items()
    }
    assert {k: v for k, v in missing.items() if v} == {}, "owner erasure handlers missing for Phase 3 tables"


def test_phase_close_pending_handler_list_is_empty() -> None:
    """Spec A1/A6: no ``PENDING_HANDLER_TABLES`` entry is allowed at phase close."""
    assert PENDING_HANDLER_TABLES == {}


def test_p3_retention_map_covers_every_personal_table() -> None:
    from app.core.hooks import autodiscover
    from app.services.privacy.retention_classes import retention_bindings

    autodiscover("app.services")

    personal = _p3_personal_tables()
    unmapped = sorted(personal - set(P3_RETENTION))
    assert unmapped == [], f"add these Phase 3 personal tables to P3_RETENTION: {unmapped}"
    stale = sorted(set(P3_RETENTION) - personal - set(NON_PERSONAL_TABLES))
    assert stale == [], f"P3_RETENTION lists tables that are not personal Phase 3 tables: {stale}"
    bindings = retention_bindings()
    wrong = {
        table: key
        for table, key in P3_RETENTION.items()
        if key != "subject" and (table not in bindings or bindings[table].class_key != key)
    }
    assert wrong == {}, f"retention class keys without a matching register_retention_binding: {wrong}"


def test_p3_bound_classes_have_default_seeds() -> None:
    """Every class a Phase 3 table is bound to is seeded per tenant (system or Phase 3 default)."""
    from app.core.hooks import autodiscover
    from app.services.privacy.retention_classes import P3_DEFAULT_CLASSES, SYSTEM_CLASSES, retention_bindings

    autodiscover("app.services")
    seeded = set(P3_DEFAULT_CLASSES) | set(SYSTEM_CLASSES)
    p3 = _p3_tables()
    unseeded = sorted(
        f"{t}->{b.class_key}" for t, b in retention_bindings().items() if t in p3 and b.class_key not in seeded
    )
    assert unseeded == []
    assert P3_DEFAULT_CLASSES["marketing_logs"][3] == 24 and P3_DEFAULT_CLASSES["marketing_logs"][5] == "delete"


# ---------------------------------------------------------------- Epic 65 (A7, FR-326): Phase 4
P4_MODEL_MODULES = tuple(
    f"app.models.{m}"
    for m in (
        "agent_runtime", "transcripts", "recording_policy", "voice", "interview_captures", "copilot", "analytics_answers",
        "screening_agent", "demand_intake", "demand_insights", "sourcing_agent", "scheduling_agent", "submission_packs",
        "chasing", "knowledge", "collection_rules", "timesheet_flags", "proposal_signals", "ai_evals",
    )
)  # fmt: skip
# Justified Phase 4 non-personal tables (staff ids, settings, platform data or no content).
P4_NON_PERSONAL: dict[str, str] = {
    "agent_principals": "agent identities per tenant",
    "agent_configs": "agent settings",
    "agent_action_policies": "autonomy per action",
    "recording_policies": "jurisdiction rules",
    "copilot_feedback": "staff ids and ratings",
    "copilot_settings": "tenant caps",
    "proposal_review_signals": "staff ids and timings only (SM-C14)",
    "ai_eval_runs": "platform evaluation data, no tenant data",
    "ai_eval_results": "platform evaluation data, no tenant data",
    "agent_shortlists": "shortlist versions (demand ids, counts)",
    "interviewer_scheduling_prefs": "staff preferences",
    "knowledge_sources": "approved content, no subject data",
    "knowledge_source_versions": "approved content, no subject data",
    "knowledge_chunks": "approved content, no subject data",
}
P4_PERSONAL_COLUMNS = ("candidate_id", "contact_id", "candidate_ids")
P4_RETENTION: dict[str, str] = {
    "agent_payloads": "agent_payloads",
    "agent_tool_calls": "agent_logs",
    "agent_proposals": "agent_logs",
    "agent_shortlist_entries": "agent_logs",
    "transcript_segments": "conversation_records",
}


def _p4_tables() -> dict[str, Table]:
    out: dict[str, Table] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if isinstance(table, Table) and mapper.class_.__module__ in P4_MODEL_MODULES:
            out[table.name] = table
    return out


def _is_p4_personal(table: Table) -> bool:
    names = set(table.c.keys())
    if any(col in names for col in P4_PERSONAL_COLUMNS):
        return True
    for mapper in Base.registry.mappers:
        if mapper.local_table is table and (
            getattr(mapper.class_, "__pii_subject__", None) or getattr(mapper.class_, "__pii_fields__", None)
        ):
            return True
    return _is_candidate_bearing(table)


def test_p4_personal_tables_are_claimed() -> None:
    claimed = registry.claimed_tables()
    missing = sorted(
        name
        for name, table in _p4_tables().items()
        if requires_rls(name)
        and _is_p4_personal(table)
        and name not in claimed
        and name not in NON_PERSONAL_TABLES
        and name not in P4_NON_PERSONAL
        and name not in PENDING_HANDLER_TABLES
    )
    assert missing == [], f"Phase 4 personal tables without an erasure handler: {missing}"


def test_p4_non_personal_list_is_honest() -> None:
    claimed = registry.claimed_tables()
    tables = _p4_tables()
    stale = sorted(t for t in P4_NON_PERSONAL if t not in tables)
    assert stale == [], f"P4_NON_PERSONAL lists unknown tables: {stale}"
    assert sorted(set(P4_NON_PERSONAL) & set(claimed)) == []


def test_erasure_order_ties_touch_disjoint_tables() -> None:
    """C-15: handlers sharing an order number must claim disjoint tables (run order is ``(order, name)``)."""
    by_order: dict[int, list[Any]] = {}
    for h in registry.handlers():
        by_order.setdefault(h.order, []).append(h)
    for order, hs in by_order.items():
        seen: dict[str, str] = {}
        for h in hs:
            for t in h.tables:
                assert t not in seen, f"order {order}: {h.name} and {seen[t]} both claim {t}"
                seen[t] = h.name
        names = [h.name for h in hs]
        assert names == sorted(names), f"order {order}: ties must run in name order: {names}"


def test_p4_retention_bindings() -> None:
    from app.core.hooks import autodiscover
    from app.services.privacy.retention_classes import retention_bindings

    autodiscover("app.services")
    bindings = retention_bindings()
    wrong = {t: k for t, k in P4_RETENTION.items() if t not in bindings or bindings[t].class_key != k}
    assert wrong == {}, f"Phase 4 retention bindings missing or different: {wrong}"
    assert bindings["proposal_review_signals"].class_key == "agent_logs"
