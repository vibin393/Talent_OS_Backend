# ruff: noqa: D401, E501
"""Phase 2 workflow catalogue (Epic 32, story 19.4, FR-167): the binding contract of trigger, action and
notification keys, the snapshot-loader registry and the human-only transition registry (FR-181).

* **Triggers** ``<domain>.<event>`` subscribe through ``subscribe_optional`` on the contract-layer event classes
  (``app.events.<domain>``), so the workflow package imports no Phase 2 domain (AD-32). Their condition fields
  come from ``register_snapshot_loader(entity_type, loader, fields)``, which the owning domain calls from its
  ``hooks.py``; until it does, a trigger offers only its ``event.*`` extras.
* **Actions** are contract keys only: each owning epic registers ``ActionDef`` under its key
  (``register_action``). ``ACTION_CATALOGUE`` names the owner of each key.
* **Human-only transitions**: ``register_human_only_transitions(entity_type, targets)``; ``register_action``
  refuses an action whose ``performs_transitions`` include one (layer 1 of the FR-181 guard).
"""

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from app.core.registry import Registry
from app.services.workflow.conditions import FieldDef
from app.services.workflow.registry import (
    FIELD_REGISTRY,
    TRIGGERS,
    Trigger,
    register_entity_fields,
    register_trigger,
)

SnapshotLoader = Callable[..., Awaitable[Mapping[str, Any] | None]]  # (ctx, db, entity_id)


# ================================================================ snapshot loaders
_loaders: Registry[str, SnapshotLoader] = Registry("workflow snapshot loader")


def register_snapshot_loader(
    entity_type: str, loader: SnapshotLoader, fields: Mapping[str, FieldDef]
) -> None:
    """A Phase 2 domain exposes its entity to workflow conditions (ids / enums only; never compensation)."""
    _loaders.register(entity_type, loader)
    if entity_type not in FIELD_REGISTRY:
        register_entity_fields(entity_type, fields)


def snapshot_loader(entity_type: str) -> SnapshotLoader | None:
    return _loaders.find(entity_type)


def snapshot_loaders() -> Mapping[str, SnapshotLoader]:
    return _loaders.view()


# ================================================================ human-only transitions (FR-181)
_HUMAN_ONLY: dict[str, set[str]] = {
    # Phase 1 + the FR-181 starred transitions of Phase 2 (owners may add more from their hooks).
    "application": {"shortlisted", "submitted", "selected", "offered", "hired", "rejected", "withdrawn"},
    "demand": {"open", "on_hold", "filled", "cancelled", "closed"},
    "approval_request": {"approved", "rejected", "changes_requested"},
    "invoice": {"issued", "written_off"},
    "payment": {"recorded"},
    "bgv_case": {"adjudicated"},
    "payroll_export": {"finalized"},
    "accounting_period": {"closed"},
    "vendor_submission": {"accepted"},
}


def register_human_only_transitions(entity_type: str, targets: Iterable[str]) -> None:
    _HUMAN_ONLY.setdefault(entity_type, set()).update(targets)


def is_human_only(entity_type: str, target: str) -> bool:
    return target in _HUMAN_ONLY.get(entity_type, set())


def human_only_transitions() -> Mapping[str, frozenset[str]]:
    return {k: frozenset(v) for k, v in _HUMAN_ONLY.items()}


# ================================================================ trigger contract
@dataclasses.dataclass(frozen=True, slots=True)
class TriggerSpec:
    key: str
    label: str
    module: str
    event_class: str
    entity_type: str
    entity_attr: str
    owner_epic: str
    extras: Mapping[str, tuple[str, FieldDef]] = dataclasses.field(default_factory=dict)
    to_status: str | None = None


def _s(label: str) -> FieldDef:
    return FieldDef("str", label)


_FROM_TO = {"from_status": ("from_status", _s("From status")), "to_status": ("to_status", _s("To status"))}

TRIGGER_CATALOGUE: tuple[TriggerSpec, ...] = (
    TriggerSpec("crm.lead_status_changed", "Lead status changed", "app.events.crm", "LeadStatusChanged", "lead", "lead_id", "15", _FROM_TO),
    TriggerSpec(
        "crm.opportunity_stage_changed", "Opportunity stage changed", "app.events.crm", "OpportunityStageChanged",
        "opportunity", "opportunity_id", "15",
        {"from_stage": ("from_stage", _s("From stage")), "to_stage": ("to_stage", _s("To stage"))},
    ),
    TriggerSpec(
        "vendors.submission_received", "Vendor submission received", "app.events.vendors", "VendorSubmissionReceived",
        "vendor_submission", "submission_id", "17", {"vendor_id": ("vendor_id", FieldDef("uuid", "Vendor"))},
    ),
    TriggerSpec("placements.status_changed", "Placement status changed", "app.events.placements", "PlacementStatusChanged", "placement", "placement_id", "19", _FROM_TO),
    TriggerSpec(
        "placements.extension_status_changed", "Extension status changed", "app.events.placements", "ExtensionStatusChanged",
        "placement", "placement_id", "31", {"to_status": ("to_status", _s("To status"))},
    ),
    TriggerSpec("bgv.case_status_changed", "BGV case status changed", "app.events.bgv", "BgvCaseStatusChanged", "bgv_case", "case_id", "20", _FROM_TO),
    TriggerSpec(
        "compliance.credential_expiring", "Credential expiring", "app.events.compliance", "CredentialStatusChanged",
        "credential", "credential_id", "21", {"credential_type": ("credential_type", _s("Credential type"))}, "expiring",
    ),
    TriggerSpec(
        "compliance.credential_expired", "Credential expired", "app.events.compliance", "CredentialStatusChanged",
        "credential", "credential_id", "21", {"credential_type": ("credential_type", _s("Credential type"))}, "expired",
    ),
    TriggerSpec("onboarding.task_overdue", "Onboarding task overdue", "app.events.onboarding", "OnboardingTaskOverdue", "onboarding_case", "case_id", "23"),
    TriggerSpec(
        "time.timesheet_submitted", "Timesheet submitted", "app.events.time", "TimesheetSubmitted", "timesheet",
        "timesheet_id", "25", {"period_end": ("period_end", FieldDef("date", "Period end"))},
    ),
    TriggerSpec(
        "time.timesheet_rejected", "Timesheet rejected", "app.events.time", "TimesheetRejected", "timesheet",
        "timesheet_id", "25", {"step_name": ("step_name", _s("Step"))},
    ),
    TriggerSpec("time.timesheet_approved", "Timesheet approved", "app.events.time", "TimesheetApproved", "timesheet", "timesheet_id", "25"),
    TriggerSpec(
        "time.timesheet_missing", "Timesheet missing", "app.events.time", "TimesheetMissing", "timesheet",
        "timesheet_id", "25", {"period_end": ("period_end", FieldDef("date", "Period end"))},
    ),
    TriggerSpec("expenses.claim_submitted", "Expense claim submitted", "app.events.expenses", "ExpenseClaimSubmitted", "expense_claim", "claim_id", "27"),
    TriggerSpec(
        "billing.invoice_issued", "Invoice issued", "app.events.billing", "InvoiceIssued", "invoice", "invoice_id", "29",
        {"client_id": ("client_id", FieldDef("uuid", "Client"))},
    ),
    TriggerSpec(
        "receivables.invoice_overdue", "Invoice overdue", "app.events.billing", "InvoiceOverdue", "invoice", "invoice_id", "30",
        {"days_overdue": ("days_overdue", FieldDef("int", "Days overdue"))},
    ),
    TriggerSpec("receivables.promise_to_pay_broken", "Promise to pay broken", "app.events.receivables", "PromiseToPayBroken", "invoice", "invoice_id", "30"),
)  # fmt: skip

# Registered by ``services/sla/hooks.py`` (this epic).
SLA_TRIGGER_KEYS = ("sla.at_risk", "sla.breached", "sla.escalation_fired")

# Contract-only action keys -> owner epic (each owner registers its ``ActionDef``).
ACTION_CATALOGUE: dict[str, str] = {
    "onboarding.create_case": "23",
    "bgv.create_case": "20",
    "documents.send_signature_request": "22",
    "vendors.release_demand_to_tier": "17",
    "comms.send_sms": "33",
    "comms.send_whatsapp": "33",
}

NOTIFICATION_KEYS = (
    "sla_clock_at_risk",
    "sla_clock_breached",
    "escalation_raised",
    "workflow_approval_requested",
    "workflow_run_failed",
)


def _to_status(value: str) -> Callable[[object], bool]:
    def check(event: object) -> bool:
        return getattr(event, "to_status", None) == value

    return check


def owner_of(trigger_key: str) -> str | None:
    for spec in (*TRIGGER_CATALOGUE, *catalogue_p3.P3_TRIGGER_CATALOGUE):
        if spec.key == trigger_key:
            return spec.owner_epic
    return "32" if trigger_key in SLA_TRIGGER_KEYS else None


def register_catalogue() -> None:
    """Register every Phase 2 and Phase 3 (``catalogue_p3``) trigger of the contract (idempotent)."""
    for spec in (*TRIGGER_CATALOGUE, *catalogue_p3.P3_TRIGGER_CATALOGUE):
        if spec.key in TRIGGERS:
            continue
        register_trigger(
            spec.key,
            Trigger(
                spec.key,
                spec.label,
                spec.module,
                spec.event_class,
                spec.entity_type,
                spec.entity_attr,
                dict(spec.extras),
                _to_status(spec.to_status) if spec.to_status else None,
            ),
        )


# Epic 36 (spec 0.7): the Phase 3 keys live in ``catalogue_p3``, which adds its actions to ``ACTION_CATALOGUE``.
# Epic 53 (spec 0.15): the Phase 4 action key lives in ``catalogue_p4``.
from app.services.workflow import (
    catalogue_p3,
    catalogue_p4,  # noqa: F401
)
