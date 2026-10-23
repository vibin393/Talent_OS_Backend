# ruff: file-ignore[line-too-long]
"""Background verification and reference checks (Epic 20, branch ``p2_bgv``, migrations ``20261005_207_01..03``).

* Configuration (internal only): ``bgv_settings`` (one row per tenant), ``bgv_packages``, ``bgv_providers``,
  ``reference_questionnaires``.
* Cases (org RLS ``worker_col=candidate_id``; children inherit ``candidate_id`` from their case / check):
  ``bgv_cases``, ``bgv_document_requests``, ``bgv_checks``, ``bgv_evidence``, ``bgv_discrepancies``.
* References: ``reference_checks`` and ``referees`` (worker sees its own), ``reference_responses`` (internal only:
  answers never reach the candidate or a portal).

``bgv_cases.status`` is written only by ``services/bgv/status.py`` (AD-4). No Aadhaar number is ever stored:
identity declarations keep ``aadhaar_last4`` only (NFR-24).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CHECK_TYPES = (
    "identity",
    "employment",
    "education",
    "address",
    "reference",
    "professional_licence",
    "criminal_record",
    "drug_test",
)
LAWFUL_TYPES = ("criminal_record", "drug_test")
PROVIDER_KINDS = ("internal", "external")
CASE_STATUSES = (
    "initiated",
    "documents_pending",
    "in_verification",
    "discrepancy",
    "under_review",
    "clear",
    "failed",
    "cancelled",
)
CASE_TERMINAL = ("clear", "failed", "cancelled")
REQUEST_KINDS = ("upload", "declaration")
REQUEST_STATUSES = ("requested", "submitted", "accepted", "returned")
CHECK_STATUSES = ("pending", "in_progress", "verified", "discrepancy", "unable_to_verify", "waived")
CHECK_FINAL = ("verified", "discrepancy", "unable_to_verify", "waived")
ASSIGNEE_KINDS = ("internal", "provider")
UPLOADER_KINDS = ("user", "provider_link", "candidate")
SEVERITIES = ("minor", "major", "critical")
DISCREPANCY_STATUSES = ("awaiting_response", "responded", "lapsed", "resolved")
REFERENCE_STATUSES = ("requested", "sent", "responded", "declined", "expired", "reviewed")
REFEREE_STATUSES = ("pending", "sent", "responded", "declined", "expired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


# ================================================================ configuration
class BgvSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    doc_reminder_every_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    discrepancy_response_working_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )
    reference_link_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    reference_reminder_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    reuse_clear_case_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("180"))
    provider_link_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("21"))


class BgvPackage(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_packages"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    turnaround_target_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lawful_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lawful_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BgvProvider(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_providers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", PROVIDER_KINDS), name="kind"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'external'"))
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    supported_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(30)), nullable=False, server_default=text("'{}'")
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ReferenceQuestionnaire(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "reference_questionnaires"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ cases
class BgvCase(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", CASE_STATUSES), name="status"),
        CheckConstraint(
            "outcome_qualifier IS NULL OR outcome_qualifier = 'with_exceptions'", name="qualifier"
        ),
        _fk("bgv_cases", "candidate_id", "candidates"),
        _fk("bgv_cases", "placement_id", "placements"),
        _fk("bgv_cases", "application_id", "applications"),
        _fk("bgv_cases", "package_id", "bgv_packages"),
        Index("ix_bgv_cases_candidate_id", "candidate_id"),
        Index("ix_bgv_cases_placement_id", "placement_id"),
        Index(
            "uq_bgv_cases_placement_package",
            "placement_id",
            "package_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled' AND placement_id IS NOT NULL"),
        ),
    )
    __audit_exclude__ = ("package_snapshot",)

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    package_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    package_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'initiated'"))
    outcome_qualifier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    adjudication_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjudicated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_purge_after: Mapped[date | None] = mapped_column(Date, nullable=True)
    evidence_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    documents_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_doc_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class BgvCaseEvent(UUIDPk, TenantMixin, Base):
    """Case timeline (status changes and notable actions; ids and codes only)."""

    __tablename__ = "bgv_case_events"
    __table_args__ = (
        _fk("bgv_case_events", "case_id", "bgv_cases", "CASCADE"),
        Index("ix_bgv_case_events_case_id", "case_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BgvDocumentRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_document_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("case_id", "key", name="uq_bgv_document_requests_case_key"),
        CheckConstraint(_in("kind", REQUEST_KINDS), name="kind"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        _fk("bgv_document_requests", "case_id", "bgv_cases", "CASCADE"),
        Index("ix_bgv_document_requests_case_id", "case_id"),
    )
    __audit_exclude__ = ("declared",)

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'requested'"))
    file_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    declared: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    return_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    redaction_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BgvCheck(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_checks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("type", CHECK_TYPES), name="type"),
        CheckConstraint(_in("status", CHECK_STATUSES), name="status"),
        CheckConstraint(
            "assignee_kind IS NULL OR " + _in("assignee_kind", ASSIGNEE_KINDS), name="assignee_kind"
        ),
        _fk("bgv_checks", "case_id", "bgv_cases", "CASCADE"),
        _fk("bgv_checks", "provider_id", "bgv_providers"),
        Index("ix_bgv_checks_case_id", "case_id"),
    )
    __audit_exclude__ = ("provider_proposal",)

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    subject_label: Mapped[str] = mapped_column(String(300), nullable=False)
    document_request_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("'{}'")
    )
    turnaround_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    halted_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    assignee_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    connector_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    start_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    result_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_contacted: Mapped[str | None] = mapped_column(String(300), nullable=True)
    verified_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    waived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reference_check_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class BgvEvidence(UUIDPk, TenantMixin, Base):
    __tablename__ = "bgv_evidence"
    __table_args__ = (
        _fk("bgv_evidence", "check_id", "bgv_checks", "CASCADE"),
        _fk("bgv_evidence", "file_id", "stored_files"),
        Index("ix_bgv_evidence_check_id", "check_id"),
    )

    check_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    uploaded_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BgvDiscrepancy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_discrepancies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        CheckConstraint(_in("status", DISCREPANCY_STATUSES), name="status"),
        _fk("bgv_discrepancies", "case_id", "bgv_cases", "CASCADE"),
        _fk("bgv_discrepancies", "check_id", "bgv_checks", "CASCADE"),
        Index("ix_bgv_discrepancies_case_id", "case_id"),
    )
    __audit_exclude__ = ("candidate_response",)

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    check_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'awaiting_response'")
    )
    response_due_on: Mapped[date] = mapped_column(Date, nullable=False)
    candidate_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_file_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raised_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ references
class ReferenceCheck(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "reference_checks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", REFERENCE_STATUSES), name="status"),
        CheckConstraint("min_referees BETWEEN 1 AND 10", name="min_referees"),
        _fk("reference_checks", "candidate_id", "candidates"),
        _fk("reference_checks", "case_id", "bgv_cases"),
        _fk("reference_checks", "check_id", "bgv_checks"),
        Index("ix_reference_checks_candidate_id", "candidate_id"),
    )
    __audit_exclude__ = ("questionnaire_snapshot",)

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    check_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questionnaire_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questionnaire_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    min_referees: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'requested'"))
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Referee(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Third-party personal data (name, email, phone): erased with the candidate's DSR."""

    __tablename__ = "referees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", REFEREE_STATUSES), name="status"),
        _fk("referees", "reference_check_id", "reference_checks", "CASCADE"),
        Index("ix_referees_reference_check_id", "reference_check_id"),
    )
    __audit_exclude__ = ("name", "email", "phone")

    reference_check_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship: Mapped[str | None] = mapped_column(String(100), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferenceResponse(UUIDPk, TenantMixin, Base):
    __tablename__ = "reference_responses"
    __table_args__ = (
        UniqueConstraint("referee_id", name="uq_reference_responses_referee_id"),
        _fk("reference_responses", "referee_id", "referees", "CASCADE"),
        _fk("reference_responses", "reference_check_id", "reference_checks", "CASCADE"),
        Index("ix_reference_responses_reference_check_id", "reference_check_id"),
    )
    __candidate_pii__ = True

    referee_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_check_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
