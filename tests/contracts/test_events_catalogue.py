"""AD-36 event catalogue (Epic 13, story 13.10 + R9): every event exists with exactly the frozen fields, is a
frozen slotted dataclass with ``tenant_id`` first, and carries only ids / str / date / bool / int / tuples
(Epic 36 adds the Phase 3 catalogue and ``datetime``, spec 0.4)."""

import dataclasses
import datetime
import importlib
import types
import typing
import uuid

import pytest

CATALOGUE: dict[str, dict[str, tuple[str, ...]]] = {
    "bgv": {
        "BgvCaseAdjudicated": ("case_id", "candidate_id", "placement_id", "outcome", "with_exceptions"),
        "BgvCaseOpened": ("case_id", "candidate_id", "placement_id", "package_id", "required"),
        "BgvCaseStatusChanged": ("case_id", "candidate_id", "placement_id", "from_status", "to_status"),
        "BgvDiscrepancyRaised": ("discrepancy_id", "case_id", "severity"),
    },
    "billing": {
        "CreditNoteIssued": ("credit_note_id", "invoice_id", "client_id", "rebill"),
        "DebitNoteIssued": ("debit_note_id", "invoice_id", "client_id"),
        "EinvoiceStatusChanged": ("document_kind", "document_id", "status"),
        "InvoiceCancelled": ("invoice_id", "cancel_basis"),
        "InvoiceIssued": ("invoice_id", "client_id", "legal_entity_id", "number", "doc_date"),
        "InvoiceOverdue": ("invoice_id", "client_id", "days_overdue"),
        "InvoiceRunCompleted": ("run_id", "legal_entity_id", "draft_count", "exception_count"),
        # Epic 51 domain-local.
        "DocumentDscSigned": ("kind", "doc_id"),
    },
    "commercials": {
        "ContractStatusChanged": (
            "contract_id",
            "client_id",
            "vendor_id",
            "from_status",
            "to_status",
            "end_date",
        ),
        "PoConsumptionThresholdReached": ("po_id", "client_id", "pct"),
        "PoValidityChanged": ("po_id", "client_id", "status", "valid_from", "valid_to", "remaining_amount"),
        "RateCardPublished": ("rate_card_id", "client_id", "contract_id", "effective_from"),
        # Epic 16 domain-local events.
        "ContractCreated": ("contract_id", "client_id", "vendor_id", "contract_type"),
        "ContractRenewalDue": ("contract_id", "client_id", "owner_id", "end_date", "lead_days"),
        "RateExceptionDecided": ("rate_check_id", "subject_kind", "subject_id", "outcome"),
        "PoExpiring": ("po_id", "client_id", "valid_to", "days_left"),
    },
    "compliance": {
        "ComplianceRequirementsEvaluated": (
            "subject_kind",
            "subject_id",
            "candidate_id",
            "placement_id",
            "bgv_package_ids",
            "credential_type_keys",
            "document_categories",
            "policy_template_ids",
        ),
        "CredentialRecorded": ("credential_id", "candidate_id", "type_key"),
        "CredentialStatusChanged": (
            "credential_id",
            "candidate_id",
            "credential_type",
            "from_status",
            "to_status",
            "mandatory",
            "mandatory_for_placement_ids",
        ),
        "CredentialVerified": ("credential_id", "candidate_id", "type_key", "verification_status"),
        "PlacementComplianceChanged": ("placement_id", "compliant", "blocking"),
    },
    "connectors": {
        "ConnectorJobFailed": ("job_id", "category", "op", "subject_kind", "subject_id"),
        "ConnectorJobQueued": ("job_id", "category", "op"),
    },
    "crm": {
        "LeadStatusChanged": ("lead_id", "from_status", "to_status"),
        "OpportunityStageChanged": ("opportunity_id", "from_stage", "to_stage"),
        "OpportunityWon": ("opportunity_id", "client_id"),
        # Epic 15 domain-local events (RECONCILED CONTRACT of spec-p2-sales-crm.md).
        "LeadCreated": ("lead_id", "owner_id", "actor_kind"),
        "LeadConverted": ("lead_id", "opportunity_id", "client_id"),
        "LeadAssigned": ("lead_id", "owner_id", "actor_id"),
        "OpportunityCreated": ("opportunity_id", "client_id", "owner_id"),
        "OpportunityAssigned": ("opportunity_id", "owner_id", "actor_id"),
        "OpportunityLost": ("opportunity_id", "reason_code"),
        "OpportunityWonHandoff": ("opportunity_id", "account_manager_id", "actor_id"),
    },
    "documents": {
        "SignatureRequestCompleted": (
            "request_id",
            "subject_kind",
            "subject_id",
            "document_id",
            "final_sha256",
        ),
        "SignatureRequestStatusChanged": (
            "request_id",
            "subject_kind",
            "subject_id",
            "from_status",
            "to_status",
        ),
    },
    "expenses": {
        "ExpenseClaimDecided": ("claim_id", "placement_id", "outcome"),
        "ExpenseClaimSubmitted": ("claim_id", "placement_id"),
    },
    "external_auth": {
        "ClientUserAccessChanged": ("client_user_id", "client_id", "status"),
        "VendorUserStatusChanged": ("vendor_user_id", "vendor_id", "status"),
    },
    "legal_entities": {
        "AccountingPeriodClosed": ("legal_entity_id", "year", "month", "actor_kind", "actor_id"),
        "AccountingPeriodReopened": ("legal_entity_id", "year", "month", "actor_kind", "actor_id", "reason"),
        "GstRegistrationChanged": ("gst_registration_id", "legal_entity_id"),
        "LegalEntityCreated": ("legal_entity_id",),
        "LegalEntityDeactivated": ("legal_entity_id",),
        # Epic 51 domain-local.
        "FxFeedFailed": ("on_date", "consecutive_days"),
    },
    "onboarding": {
        "OnboardingCaseCancelled": ("case_id", "placement_id"),
        "OnboardingCaseCompleted": ("case_id", "placement_id"),
        "OnboardingCaseCreated": ("case_id", "placement_id", "candidate_id"),
        "OnboardingProgressChanged": ("case_id", "placement_id", "pct"),
        "OnboardingTaskOverdue": ("task_id", "case_id"),
        "OnboardingTaskReturned": ("task_id", "case_id", "candidate_id"),
        "PolicyAcknowledged": ("candidate_id", "template_id", "template_version_id"),
    },
    "payroll": {
        "PayPeriodLocked": ("pay_period_id", "legal_entity_id"),
        "PayPeriodReopened": ("pay_period_id", "legal_entity_id", "reason"),
        "PayrollExportFinalized": ("export_version_id", "pay_period_id", "legal_entity_id"),
        "VendorStatementGenerated": ("statement_id", "vendor_id", "pay_period_id"),
    },
    "placements": {
        "BankDetailsChangeDecided": ("candidate_id", "bank_account_id", "outcome"),
        "BenchEntryStatusChanged": ("bench_entry_id", "candidate_id", "from_status", "to_status"),
        "ExtensionConfirmed": ("extension_id", "placement_id", "new_end_date", "rate_segment_id"),
        "ExtensionStatusChanged": ("extension_id", "placement_id", "from_status", "to_status"),
        "OffboardingCaseStatusChanged": ("case_id", "placement_id", "from_status", "to_status"),
        "PlacementActivated": ("placement_id", "candidate_id", "actual_start_date"),
        "PlacementConverted": (
            "placement_id",
            "conversion_id",
            "client_id",
            "conversion_date",
            "fee_amount",
            "currency",
        ),
        "PlacementCreated": (
            "placement_id",
            "candidate_id",
            "client_id",
            "vendor_id",
            "legal_entity_id",
            "offer_id",
            "demand_id",
        ),
        "PlacementDropped": ("placement_id", "demand_id", "reason"),
        "PlacementEnded": (
            "placement_id",
            "candidate_id",
            "end_date",
            "reason",
            "worker_type",
            "bench_eligible",
        ),
        "PlacementStatusChanged": ("placement_id", "from_status", "to_status", "reason"),
        "RateSegmentAdded": (
            "placement_id",
            "rate_segment_id",
            "effective_from",
            "previous_segment_id",
            "superseded",
        ),
        "StartGateStatusChanged": ("placement_id", "gate_kind", "source_ref", "status"),
    },
    "receivables": {
        "DisputeStatusChanged": ("dispute_id", "invoice_id", "from_status", "to_status"),
        "InvoiceWrittenOff": ("write_off_id", "document_kind", "document_id"),
        "PaymentRecorded": ("payment_id", "client_id", "legal_entity_id"),
        "PaymentReversed": ("payment_id", "reason"),
        "PromiseToPayBroken": ("promise_id", "invoice_id", "client_id"),
        # Epic 51 domain-local.
        "TdsReconRunCompleted": ("run_id",),
    },
    "time": {
        "LeaveRequestDecided": ("leave_request_id", "candidate_id", "outcome"),
        "TimesheetApproved": ("timesheet_id", "placement_id"),
        "TimesheetMissing": ("timesheet_id", "placement_id", "period_end"),
        "TimesheetRejected": ("timesheet_id", "placement_id", "step_name"),
        "TimesheetSubmitted": ("timesheet_id", "placement_id", "period_start", "period_end"),
    },
    # ---------------------------------------------------------------- Phase 3 (Epic 36, spec 0.4)
    "skills": {"SkillGraphChanged": ("scope", "skill_keys")},
    "assessments": {
        "AssessmentScored": (
            "invitation_id",
            "assessment_id",
            "candidate_id",
            "application_id",
            "score",
            "skill_scores",
            "source",
        ),
        "AssessmentInvitationDecided": (
            "invitation_id",
            "assessment_id",
            "candidate_id",
            "application_id",
            "context_kind",
            "context_id",
            "outcome",
            "gating",
            "actor_kind",
        ),
    },
    "talent_pools": {
        "PoolMembershipChanged": ("pool_id", "refresh_id", "added_count", "removed_count", "changed_at")
    },
    "campaigns": {
        "CampaignMessageEngaged": (
            "campaign_message_id",
            "campaign_id",
            "enrollment_id",
            "candidate_id",
            "kind",
            "occurred_at",
        )
    },
    "mobility": {
        "InternalTransferConfirmed": (
            "application_id",
            "demand_id",
            "candidate_id",
            "employee_kind",
            "user_id",
            "from_placement_id",
            "new_placement_id",
        )
    },
    "referrals": {
        "ReferralHired": (
            "referral_id",
            "candidate_id",
            "application_id",
            "placement_id",
            "referrer_kind",
            "referrer_id",
        )
    },
    "projects": {
        "DeliverableAccepted": ("deliverable_id", "project_id", "client_id", "milestone_id", "actor_kind"),
        "ProjectAssignmentEnded": (
            "assignment_id",
            "project_id",
            "project_role_id",
            "placement_id",
            "candidate_id",
            "ended_on",
        ),
        # Epic 45 domain-local.
        "DeliverableSubmitted": ("deliverable_id", "project_id", "client_id"),
        "DeliverableRejected": ("deliverable_id", "project_id", "client_id", "actor_kind"),
        "ProjectBudgetThresholdReached": ("project_id", "dimension", "threshold", "budget_version"),
    },
    "workforce": {
        "PlanLineApproved": ("workforce_plan_id", "plan_line_id", "client_id", "headcount"),
        # Epic 44 domain-local.
        "WorkforcePlanSubmitted": (
            "workforce_plan_id",
            "revision_no",
            "approval_request_id",
            "actor_user_id",
        ),
        "WorkforcePlanDecided": ("workforce_plan_id", "revision_no", "outcome", "decided_by"),
        "WorkforceSlotFilled": ("workforce_plan_id", "slot_id", "placement_id"),
        "ClientForecastSubmitted": ("workforce_plan_id", "client_id"),
    },
    "immigration": {
        "PermitRecorded": (
            "permit_id",
            "immigration_case_id",
            "candidate_id",
            "placement_id",
            "country",
            "permit_type",
            "valid_from",
            "valid_to",
            "credential_type_key",
        )
    },
    "analytics": {
        "PredictionBandChanged": (
            "prediction_id",
            "model_kind",
            "subject_kind",
            "subject_id",
            "from_band",
            "to_band",
        )
    },
    "vendors": {
        "VendorReleaseClosed": ("release_id", "demand_id"),
        "VendorReleaseCreated": ("release_id", "demand_id", "vendor_ids"),
        "VendorStatusChanged": ("vendor_id", "from_status", "to_status"),
        "VendorSubmissionDecided": ("submission_id", "vendor_id", "outcome", "application_id"),
        "VendorSubmissionReceived": ("submission_id", "release_id", "vendor_id", "demand_id"),
    },
    # ---------------------------------------------------------------- Phase 4 (Epic 53, spec 0.4)
    # Epic 53 (spec 0.4): agent runtime events.
    "agents": {
        "AgentRunStarted": (
            "run_id",
            "agent_key",
            "origin",
            "trigger_kind",
            "subject_type",
            "subject_id",
            "owner_user_id",
        ),
        "AgentRunFinished": (
            "run_id",
            "agent_key",
            "status",
            "reason_code",
            "subject_type",
            "subject_id",
            "owner_user_id",
        ),
        "AgentRunFailed": (
            "run_id",
            "agent_key",
            "reason_code",
            "subject_type",
            "subject_id",
            "owner_user_id",
            "fallback_task_id",
        ),
        "ProposalCreated": (
            "proposal_id",
            "run_id",
            "agent_key",
            "tool_key",
            "origin",
            "target_type",
            "target_id",
            "assignee_user_id",
            "route_role_key",
            "expires_at",
        ),
        "ProposalDecided": (
            "proposal_id",
            "run_id",
            "agent_key",
            "tool_key",
            "outcome",
            "status",
            "decided_by",
            "target_type",
            "target_id",
        ),
        "KillSwitchChanged": ("scope", "on", "actor_id"),
        "AgentBudgetThresholdCrossed": ("agent_key", "period", "threshold", "used", "budget"),
    },  # fmt: skip
    "conversations": {
        "ScreeningConversationCompleted": (
            "conversation_id",
            "application_id",
            "candidate_id",
            "outcome",
            "transcript_id",
            "run_id",
        )
    },
    "voice": {
        "VoiceSessionReady": (
            "voice_session_id",
            "candidate_id",
            "application_id",
            "transcript_id",
            "recording_file_id",
        )
    },
    "interview_captures": {
        "InterviewCaptureReady": ("capture_id", "interview_id", "candidate_id", "transcript_id")
    },
    # Epic 60 (Interview Assistant): domain-local decision event (FR-323 selection data).
    "interview_assistant": {
        "DraftRatingsDecided": ("draft_id", "interview_id", "candidate_id", "accepted", "changed", "ignored")
    },
    "scheduling": {
        "SlotPicked": (
            "proposal_ref",
            "interview_id",
            "application_id",
            "candidate_id",
            "slot_start",
            "slot_end",
            "run_id",
        )
    },
    # Epic 55 (Demand Agent). ``DemandIntakeMatched`` subclasses ``DemandIntakeReceived`` (the agent trigger).
    "demand_intake": {
        "DemandIntakeReceived": ("message_id", "source", "status", "matched_client_id", "owner_user_id", "attempt"),
        "DemandIntakeMatched": ("message_id", "source", "status", "matched_client_id", "owner_user_id", "attempt"),
        "DemandDuplicateFlagged": ("flag_id", "demand_id", "related_demand_id", "score"),
        "DemandFlagResolved": ("flag_id", "demand_id", "kind", "resolution", "resolved_by"),
    },
    # Epic 56 (Sourcing & Matching Agent): domain-local events; the last two are the agent's event triggers.
    "sourcing_agent": {
        "AgentShortlistSaved": ("shortlist_id", "demand_id", "run_id", "entry_count", "new_count", "dropped_count"),
        "AgentShortlistEntryDecided": (
            "entry_id",
            "shortlist_id",
            "demand_id",
            "candidate_id",
            "decision",
            "actor_kind",
            "via_proposal",
        ),
        "SourcingRerankRequested": ("demand_id", "match_run_id"),
        "SourcingRefreshDue": ("demand_id", "week"),
    },
    # Epic 62 (Compliance, BGV & Onboarding Agents): domain-local events.
    "chasing": {"ChaseEscalated": ("candidate_id", "placement_id", "reason", "task_id", "item_count")},
    "knowledge": {
        "KnowledgeSourcePublished": ("source_id", "version_no", "audience"),
        "KnowledgeSourceUnpublished": ("source_id",),
        "OnboardingQuestionAsked": ("thread_id", "candidate_id", "message_id"),
        "OnboardingQuestionHandedOff": ("thread_id", "candidate_id", "task_id", "topic", "reason"),
    },
    # Epic 63 (Timesheet & Billing/Collections Agents): domain-local events.
    "timesheet_flags": {
        "TimesheetFlagsRaised": ("timesheet_id", "placement_id", "flag_count", "max_severity", "timesheet_version")
    },
    "collection_rules": {
        "CollectionsAgentHoldChanged": ("client_id", "document_id", "mode", "active", "actor_id"),
        "CollectionMessageSent": ("client_id", "document_ids", "email_message_id", "tone", "via_proposal_id"),
    },
    # Epic 61 (Submission Agent): domain-local pack events.
    "submission_packs": {
        "SubmissionPackReady": (
            "pack_id",
            "application_id",
            "demand_id",
            "client_id",
            "status",
            "blocker_count",
            "agent_run_id",
        ),
        "SubmissionPackUsed": ("pack_id", "submission_id", "application_id", "changed_fields", "note_change"),
    },
    "transcripts": {
        "TranscriptReady": (
            "transcript_id",
            "source_kind",
            "source_id",
            "candidate_id",
            "application_id",
            "interview_id",
        )
    },
    # Epic 66 (Phase 5 foundation, spec 0.4).
    "ecosystem": {
        "InstallationChanged": (
            "installation_id", "kind", "app_id", "connector_key", "change", "status", "actor_kind",
        ),
        "EcosystemKillSwitchChanged": ("scope", "on", "app_id", "actor_id"),
        "OAuthTokenFamilyRevoked": ("installation_id", "family_id", "user_id", "reason"),
        "UserGrantChanged": ("installation_id", "user_id", "resource", "change"),
        "ConnectorSyncFailed": ("installation_id", "config_id", "job_key", "error_code", "consecutive_failures"),
        "ConnectorHealthChanged": ("installation_id", "health_status", "error_code"),
    },
    # Epic 68 (Webhooks, spec A5).
    "webhooks": {
        "WebhookSubscriptionChanged": (
            "subscription_id", "owner_kind", "installation_id", "change", "status", "disabled_reason",
        ),
        "WebhookDeliveryFailed": ("subscription_id", "delivery_id", "event_type", "attempt", "final"),
    },
    # Epic 73 (Governed MCP Server, spec A5).
    "mcp": {
        "McpEnablementChanged": ("on", "tool_groups", "notice_version", "actor_id"),
        "McpSessionOpened": ("session_id", "run_id", "installation_id", "user_id"),
        "McpSessionEnded": ("session_id", "run_id", "installation_id", "user_id", "reason"),
        "McpSessionPaused": ("session_id", "run_id", "user_id", "reason", "distinct_candidates"),
    },
}  # fmt: skip

ALLOWED = (uuid.UUID, str, datetime.date, datetime.datetime, bool, int)  # datetime: Phase 3 (spec 0.4)


def _allowed(tp: object) -> bool:
    if tp in ALLOWED or tp is type(None):
        return True
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        return all(_allowed(a) for a in typing.get_args(tp))
    if origin is typing.Literal:
        return all(isinstance(a, str) for a in typing.get_args(tp))
    if origin is tuple:
        return all(a is Ellipsis or _allowed(a) for a in typing.get_args(tp))
    return False


@pytest.mark.parametrize("module", sorted(CATALOGUE))
def test_catalogue_module(module: str) -> None:
    mod = importlib.import_module(f"app.events.{module}")
    declared = {
        name
        for name, obj in vars(mod).items()
        if isinstance(obj, type) and obj.__module__ == mod.__name__ and dataclasses.is_dataclass(obj)
    }
    assert declared == set(CATALOGUE[module])
    for name, fields in CATALOGUE[module].items():
        cls = getattr(mod, name)
        params = cls.__dataclass_params__
        assert params.frozen, name
        assert hasattr(cls, "__slots__"), name
        names = [f.name for f in dataclasses.fields(cls)]
        assert names[0] == "tenant_id", name
        assert tuple(names[1:]) == fields, name
        hints = typing.get_type_hints(cls)
        assert hints["tenant_id"] is uuid.UUID
        for field in names:
            assert _allowed(hints[field]), f"{name}.{field}: {hints[field]!r}"
