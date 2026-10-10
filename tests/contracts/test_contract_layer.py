"""AD-36 contract layer (Epic 13, story 13.10): service stubs, frozen signatures and DTO fields, hub-table guards.

``signatures.json`` is the frozen snapshot: changing a contract signature or DTO field set needs a spine
amendment and a deliberate snapshot update (``python -m tests.contracts.test_contract_layer`` rewrites it).
"""

import importlib
import inspect
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

SNAPSHOT = Path(__file__).with_name("signatures.json")

# Contract stubs: (module, function, filling epic). Each raises NotImplementedError until its epic lands.
STUBS: list[tuple[str, str]] = [
    # Every Phase 2 contract stub is filled (phase close): payroll (28), documents (22), vendors (17),
    # compliance (21), time (25), billing (29) and receivables (30).
    # Phase 3 (Epic 36, AD-39): every Phase 3 contract stub is filled (Epic 40 filled the last two).
    # Phase 4 (Epic 53, AD-55, spec 0.12): filled by Epics 57, 59, 61 and 62.
    # Epic 57 filled screening_agent_service.extract_answers (signature still frozen; see IMPLEMENTED).
    # Epic 59 filled scheduling_agent_service.propose (signature still frozen; see IMPLEMENTED).
    # Epic 62 filled knowledge_service.search (signature still frozen; see IMPLEMENTED).
]

# Implemented in Epic 13 and callable by every Phase 2 epic.
IMPLEMENTED: list[tuple[str, str]] = (
    # Epic 53 (Phase 4 foundation, spec 0.13): transcripts and the Recording Policy gate.
    [
        ("app.services.transcripts_service", n)
        for n in ("create", "append_segments", "ingest_upload", "get", "for_model", "erase")
    ]
    + [("app.services.recording_policy.gate", n) for n in ("check", "seed_tenant_defaults")]
    + [("app.services.privacy.retention_classes", "seed_p4_classes")]
    # Epic 57 filled the screening agent contract stub (signature still frozen).
    + [("app.services.screening_agent_service", "extract_answers")]
    # Epic 59 filled the scheduling agent contract stub (signature still frozen).
    + [("app.services.scheduling_agent_service", "propose")]
    # Epic 61 filled the submission packs contract stub (signature still frozen).
    + [("app.services.submission_packs_service", "prepare_pack")]
    # Epic 62 filled the knowledge contract stub (signature still frozen).
    + [("app.services.knowledge_service", "search")]
    +
    # Epic 36 (Phase 3 foundation, spec 0.5): implemented and callable by every Phase 3 epic.
    [
        ("app.services.demands_service", "create_draft"),
        ("app.services.placements_service", "link_project"),
        ("app.services.privacy.consent", "register_default_notice"),
        # Epic 37 filled the skills graph contract stub (signature still frozen).
        ("app.services.skills_service", "expand"),
        # Epic 45 filled the projects contract stub (signature still frozen).
        ("app.services.projects_service", "project_context"),
        # Epic 38 filled the segments / pools contract stubs (signatures still frozen).
        ("app.services.segments_service", "evaluate"),
        ("app.services.pools_service", "member_ids"),
        ("app.services.pools_service", "pool_summary"),
        # Epic 40 filled the assessments contract stubs (signatures still frozen).
        ("app.services.assessments_service", "invite"),
        ("app.services.assessments_service", "get_invitation"),
    ]
    +
    # Epic 29 / Epic 30 filled the billing and receivables contract stubs (signatures still frozen).
    [("app.services.billing_service", n) for n in ("lock_for_settlement", "get_receivable_document")]
    + [("app.services.receivables_service", "open_balance")]
    +
    # Epic 22 filled the documents contract stubs (signatures still frozen).
    [
        ("app.services.documents_service", n)
        for n in ("render_pdf", "create_signature_request", "get_signature_request")
    ]
    +
    # Epic 28 filled the payroll contract stub (signature still frozen).
    [("app.services.payroll_service", "pay_period_for")]
    +
    # Epic 25 filled the time contract stubs (signatures still frozen).
    [("app.services.time_service", n) for n in ("billing_inputs", "pay_inputs")]
    +
    # Epic 21 filled the compliance contract stub (signature still frozen).
    [("app.services.compliance_service", "worker_compliance")]
    +
    # Epic 17 filled the vendors contract stubs (signatures still frozen).
    [
        ("app.services.vendors_service", n)
        for n in ("get_vendor_summary", "assert_vendor_can_submit", "sourcing_for_application")
    ]
    +
    # Epic 19 filled the placements contract stubs (signatures still frozen).
    [
        ("app.services.placements_service", n)
        for n in (
            "create_from_offer",
            "create_direct",
            "get_placement_summary",
            "billing_context",
            "pay_context",
            "rate_segment_on",
            "get_rate_segment",
            "worker_payment_identity",
        )
    ]
    +  # Epic 16 filled the commercials contract stubs (signatures still frozen).
    [
        ("app.services.commercials_service", n)
        for n in (
            "validate_rate",
            "record_po_consumption",
            "po_balance",
            "active_contract_for",
            "create_contract_draft",
        )
    ]
    + [
        ("app.services.legal_entities_service", name)
        for name in (
            "get_entity",
            "assert_accepts_new_documents",
            "default_registration_for",
            "validate_gstin",
            "provision_default_entity",
            "get_gst_registration",
            "get_lut",
            "series_for",
            "bank_account_for_documents",
            "ensure_periods",
            "assert_open",
            "checklist",
            "close",
            "reopen",
            "register_close_check",
            "rate_for",
            "upsert_rate",
        )
    ]
    + [
        ("app.services.legal_entities.numbering", n)
        for n in (
            "assign",
            "preview_next",
            "raise_counter_to",
            "validate_series_config",
            "render",
            "max_rendered_length",
        )
    ]
    + [
        ("app.services.postings", n)
        for n in (
            "register_posting_source",
            "load_unposted",
            "reserve",
            "record_posted",
            "post",
            "release",
            "targets_for",
            "sources_for",
            "has_active_posting",
            "active_postings_for",
        )
    ]
    + [
        ("app.services.approvals", n)
        for n in (
            "start",
            "decide",
            "cancel_open",
            "skip_current_step",
            "register_approval_subject",
            "register_approver_resolver",
            "register_delegate_resolver",
            "register_route_resolver",
            "resolve_route",
            "register_subject",
        )
    ]
    + [
        ("app.services.sla_service", n)
        for n in (
            "start_clock",
            "stop_clock",
            "cancel_clock",
            "clocks_for",
            "compute_status",
            "register_sla_milestone",
        )
    ]
    + [
        ("app.services.connectors.registry", n)
        for n in ("get", "register_adapter", "register_webhook_handler")
    ]
    + [("app.services.connectors.jobs", n) for n in ("enqueue", "pending", "retry")]
    + [
        ("app.services.imports_service", n)
        for n in ("register_preview", "mark_applied", "mark_failed", "discard")
    ]
    + [
        ("app.services.placements_service", n)
        for n in (
            "register_start_gate_kind",
            "start_gate_kinds",
            "register_offboarding_verifier",
            "offboarding_verifiers",
        )
    ]
    + [
        ("app.services.privacy.retention_classes", n)
        for n in ("get_class", "list_classes", "register_retention_binding")
    ]
    + [("app.services.workflow.registry", n) for n in ("register_condition", "evaluate_condition")]
    + [
        ("app.services.tasks_service", "create_system_task"),
        ("app.core.org_grants", "grant"),
        ("app.core.org_grants", "revoke"),
        ("app.core.org_grants", "has_grant"),
        ("app.core.client_scope", "visible_placement_ids"),
        ("app.core.client_scope", "visible_demand_ids"),
        ("app.core.reconciliation", "register_reconciliation_check"),
        ("app.core.reconciliation", "reconciliation_checks"),
        ("app.core.actors", "actor_from_ctx"),
        ("app.core.actors", "is_human"),
        ("app.core.actors", "require_human"),
        ("app.core.encrypted", "seal"),
        ("app.core.encrypted", "blind_index"),
        ("app.core.encrypted", "set_identity_field"),
        ("app.core.encrypted", "unmask"),
    ]
    # Phase 4 (Epic 53, spec 0.5 / 0.7 / 0.8): core.agentic registries, the gateway step API and the runtime facade.
    + [
        ("app.core.agentic.tools", n)
        for n in ("register_tool", "get_tool", "get_tool_by_wire", "tools", "validate_catalogue")
    ]
    + [
        ("app.core.agentic.agents", n)
        for n in ("register_agent", "get_agent", "agents", "register_agent_trigger", "agent_triggers")
    ]
    + [
        ("app.core.agentic.copilot", n)
        for n in ("register_copilot_capability", "copilot_capabilities", "enabled_capabilities")
    ]
    + [("app.core.hooks", "register_post_discovery")]
    + [
        ("app.services.ai.loop", n)
        for n in ("run_agent_step", "stream_agent_step", "wrap_tool_result", "tool_result_block")
    ]
    + [("app.services.ai.model_tiers", "model_for"), ("app.services.ai.pricing", "cost_micros")]
    + [
        ("app.services.agent_runtime_service", n)
        for n in (
            "start_run",
            "resume",
            "cancel_run",
            "get_run",
            "list_runs",
            "read_payload",
            "purge_run_content",
            "list_proposals",
            "get_proposal",
            "decide_proposal",
            "bulk_decide",
            "confirm_draft",
            "discard_draft",
            "create_draft_for",
            "effective_mode",
            "get_agents",
            "acknowledge_enablement",
            "agentic_state",
            "set_agent_enabled",
            "update_config",
            "set_action_policy",
            "set_kill_switch",
            "platform_set_kill_switch",
            "tool_catalogue",
            "register_grounded_conversation_checker",
            "scope_context",
        )
    ]  # fmt: skip
)

# DTOs (pydantic, frozen): module -> class names.
DTOS: dict[str, tuple[str, ...]] = {
    "app.schemas.placements": (
        "PlacementSummary",
        "PlacementBillingContext",
        "PlacementPayContext",
        "RateSegmentOut",
        "PlacementCreateIn",
        "WorkerPaymentIdentity",
    ),
    "app.schemas.commercials": (
        "RateCheckRequest",
        "RateCheckResult",
        "PoBalance",
        "ContractSummary",
        "ContractDraftIn",
    ),
    "app.schemas.time": ("TimeBillingInputs", "TimePayInputs"),
    "app.schemas.billing": ("SettlementFacts", "ReceivableDocumentSummary"),
    "app.schemas.payroll": ("PayPeriodRef",),
    "app.schemas.documents": (
        "RenderedFile",
        "SignatureRequestCreateIn",
        "SignerSpec",
        "SignatureRequestSummary",
    ),
    "app.schemas.vendors": ("VendorSummary", "VendorSourcing"),
    "app.schemas.compliance": ("WorkerComplianceStatus",),
    # Phase 3 (Epic 36, spec 0.5)
    "app.schemas.segments": ("SegmentEvaluation",),
    "app.schemas.talent_pools": ("PoolSummary",),
    "app.schemas.assessments": ("AssessmentInviteIn", "AssessmentInvitationSummary"),
    "app.schemas.projects": ("ProjectContext", "ProjectRoleRef"),
    # Phase 4 (Epic 53, spec 0.12 / 0.13)
    "app.schemas.screening_agent": ("ExtractAnswersIn", "ExtractedAnswer", "ExtractedAnswersOut"),
    "app.schemas.scheduling_agent": ("SlotProposalIn", "Slot", "SlotProposalOut"),
    "app.schemas.submission_packs": ("PackCheck", "SubmissionPackOut"),
    "app.schemas.knowledge": ("KnowledgeHit",),
    "app.schemas.transcripts": ("TranscriptSegmentIn", "TranscriptSegmentOut", "TranscriptOut"),
    "app.schemas.agentic": (
        "ModelToolDef",
        "StepRequest",
        "ToolUse",
        "StepUsage",
        "StepResult",
        "RunRef",
        "RunSummary",
        "RunOut",
        "ToolCallOut",
        "ProposalOut",
        "ModeDecision",
        "AgentCatalogOut",
        "AgenticStateOut",
        "RunPayloadOut",
        "ProposalSummary",
        "StepOut",
        "PayloadItem",
        "ToolCatalogOut",
    ),  # fmt: skip
}


def _signature(module: str, name: str) -> str:
    fn = getattr(importlib.import_module(module), name)
    return str(inspect.signature(fn))


def current_snapshot() -> dict[str, Any]:
    return {
        "functions": {f"{m}:{n}": _signature(m, n) for m, n in [*STUBS, *IMPLEMENTED]},
        "dtos": {
            f"{m}:{cls}": list(getattr(importlib.import_module(m), cls).model_fields)
            for m, classes in DTOS.items()
            for cls in classes
        },
    }


def test_signatures_match_frozen_snapshot() -> None:
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = current_snapshot()
    assert current["functions"] == frozen["functions"]
    assert current["dtos"] == frozen["dtos"]


@pytest.mark.parametrize(("module", "name"), STUBS)
async def test_stub_raises_not_implemented(module: str, name: str) -> None:
    fn = getattr(importlib.import_module(module), name)
    params = list(inspect.signature(fn).parameters.values())
    args = [None] * sum(
        1 for p in params if p.default is inspect.Parameter.empty and p.kind is p.POSITIONAL_OR_KEYWORD
    )
    kwargs = {
        p.name: None for p in params if p.default is inspect.Parameter.empty and p.kind is p.KEYWORD_ONLY
    }
    with pytest.raises(NotImplementedError, match=r"AD-(3[69]|55) contract"):
        await fn(*args, **kwargs)


@pytest.mark.parametrize("module", sorted(DTOS))
def test_dtos_are_frozen(module: str) -> None:
    for cls in DTOS[module]:
        model = getattr(importlib.import_module(module), cls)
        assert model.model_config.get("frozen") is True, cls


# ---------------------------------------------------------------- hub-table guards
async def _hub(app: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    from tests.epic3 import create_candidate
    from tests.modules.demands.helpers import create_client
    from tests.modules.identity.conftest import join, make_tenant
    from tests.modules.legal_entities.conftest import provision

    tenant = await make_tenant(seat_limit=10)
    le = await provision(tenant)
    admin = await join(app, tenant, name="Hub Admin")
    try:
        client_id = await create_client(admin)
        cand = await create_candidate(admin, full_name="Hana Hub")
    finally:
        await admin.client.aclose()
    return tenant.id, le, client_id, uuid.UUID(cand["id"])


async def _placement(tenant_id: uuid.UUID, le: uuid.UUID, client_id: uuid.UUID, cand: uuid.UUID) -> uuid.UUID:
    from app.core.database import tenant_session
    from app.models.placements import Placement

    pid = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        db.add(
            Placement(
                id=pid,
                tenant_id=tenant_id,
                human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                candidate_id=cand,
                client_id=client_id,
                legal_entity_id=le,
                worker_type="employee",
                payee_kind="worker",
                planned_start_date=date(2026, 10, 1),
                currency="INR",
            )
        )
    return pid


def _translated(exc: BaseException) -> Any:
    from app.core.db_errors import translate

    return translate(exc)  # type: ignore[arg-type]


async def test_rate_segment_overlap_and_immutability(app: Any) -> None:
    from app.core.database import tenant_session
    from app.models.placements import RateSegment

    tenant_id, le, client_id, cand = await _hub(app)
    pid = await _placement(tenant_id, le, client_id, cand)

    def seg(start: date, end: date | None) -> RateSegment:
        return RateSegment(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            placement_id=pid,
            effective_from=start,
            effective_to=end,
            rate_unit="hour",
            currency="INR",
            bill_rate=Decimal("1500.0000"),
        )

    first = seg(date(2026, 10, 1), None)
    async with tenant_session(tenant_id) as db:
        db.add(first)
        await db.flush()
        org = (
            await db.execute(
                text("SELECT client_id, candidate_id FROM rate_segments WHERE id = :i"), {"i": first.id}
            )
        ).one()
        assert (org.client_id, org.candidate_id) == (client_id, cand)  # inherited from the placement
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            db.add(seg(date(2026, 12, 1), None))
            await db.flush()
    err = _translated(exc.value)
    assert err is not None and err.code == "overlap"
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text("UPDATE rate_segments SET effective_to = '2026-11-30' WHERE id = :i"), {"i": first.id}
        )
        db.add(seg(date(2026, 12, 1), None))
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            await db.execute(text("UPDATE rate_segments SET bill_rate = 1 WHERE id = :i"), {"i": first.id})
    assert _translated(exc.value).code == "document_immutable"


async def test_placement_org_ids_immutable_and_payee_check(app: Any) -> None:
    from app.core.database import tenant_session

    tenant_id, le, client_id, cand = await _hub(app)
    pid = await _placement(tenant_id, le, client_id, cand)
    async with tenant_session(tenant_id) as db:
        await db.execute(text("UPDATE placements SET status = 'active' WHERE id = :i"), {"i": pid})
    for column, value in (
        ("client_id", uuid.uuid4()),
        ("candidate_id", uuid.uuid4()),
        ("vendor_id", uuid.uuid4()),
    ):
        with pytest.raises(DBAPIError):
            async with tenant_session(tenant_id) as db:
                await db.execute(
                    text(f"UPDATE placements SET {column} = :v WHERE id = :i"), {"v": value, "i": pid}
                )
    # payee_kind = vendor requires a vendor
    with pytest.raises(DBAPIError):
        async with tenant_session(tenant_id) as db:
            await db.execute(text("UPDATE placements SET payee_kind = 'vendor' WHERE id = :i"), {"i": pid})


async def test_invoice_immutability_period_guard_and_cancel_basis(app: Any) -> None:
    from app.core.database import tenant_session
    from app.core.fiscal import today_for
    from app.models.billing import Invoice

    tenant_id, le, client_id, _cand = await _hub(app)
    today = today_for("Asia/Kolkata")
    inv_id = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        db.add(
            Invoice(id=inv_id, tenant_id=tenant_id, legal_entity_id=le, client_id=client_id, currency="INR")
        )
        await db.flush()
        # draft: freely editable, and a NULL doc_date skips the period guard
        await db.execute(text("UPDATE invoices SET document_total = 100 WHERE id = :i"), {"i": inv_id})
        await db.execute(
            text(
                "UPDATE invoices SET status = 'issued', number = 'INV/1', doc_date = :d, issued_at = now() WHERE id = :i"
            ),
            {"d": today, "i": inv_id},
        )
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            await db.execute(text("UPDATE invoices SET document_total = 1 WHERE id = :i"), {"i": inv_id})
    assert _translated(exc.value).code == "document_immutable"
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            await db.execute(text("DELETE FROM invoices WHERE id = :i"), {"i": inv_id})
    assert _translated(exc.value).code == "document_immutable"
    # cancel needs a basis (CHECK), then the issued -> cancelled transition is allowed
    with pytest.raises(DBAPIError):
        async with tenant_session(tenant_id) as db:
            await db.execute(
                text("UPDATE invoices SET status = 'cancelled', cancelled_at = now() WHERE id = :i"),
                {"i": inv_id},
            )
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "UPDATE invoices SET status = 'cancelled', cancel_basis = 'full_credit_note', cancelled_at = now() WHERE id = :i"
            ),
            {"i": inv_id},
        )
    # period guard: an issued invoice dated in a missing period
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            db.add(
                Invoice(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    legal_entity_id=le,
                    client_id=client_id,
                    currency="INR",
                    status="issued",
                    number="INV/2",
                    doc_date=today + timedelta(days=400),
                )
            )
            await db.flush()
    err = _translated(exc.value)
    assert err is not None and err.code == "period_closed"


if __name__ == "__main__":  # pragma: no cover - deliberate snapshot update (spine amendment)
    SNAPSHOT.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
