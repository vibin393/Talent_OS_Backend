# ruff: file-ignore[line-too-long]
"""Immigration (Epic 46, branch ``p3_immigration``, migrations ``20261007_310_01..04``; AD-52, FR-232..236).

* ``visa_type_templates`` -- platform reference data (no RLS, SELECT-only for ``talent_app``): US H-1B, UK Skilled
  Worker, UAE employment visa. Copied into tenant ``visa_types`` on adoption; tenant edits never change them.
* ``visa_types`` / ``visa_type_versions`` -- tenant configuration. Versions are append-only (immutability trigger);
  a Case pins the version it was opened on. Tenant RLS only (no PII; the Worker portal reads stage labels).
* ``immigration_counsel`` -- a small counsel directory (P1 Contacts require a Client). Internal only.
* ``immigration_cases`` -- outer state ``open -> in_stage -> approved* | refused* | withdrawn`` with the configured
  stage key as sub-status. Receipt number ``EncryptedStr`` + ``_last4`` (AD-35). Worker org RLS on ``candidate_id``.
* ``case_events`` (append-only timeline; worker org RLS so a portal upload can write its row) and
  ``case_dependants`` (passport ``EncryptedStr``, internal only).
* ``case_document_requests`` -- checklist items pointing at P2 ``documents`` (AD-28). Worker org RLS.
* ``permits`` -- recorded on an Approved outcome (number ``EncryptedStr`` + last4 + blind index); status derived.
  Worker org RLS.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

VISA_CATEGORIES = ("work", "dependent", "business_travel")
STAGE_CATEGORIES = ("preparing", "filed", "pending_decision")
CASE_STATUSES = ("open", "in_stage", "approved", "refused", "withdrawn")
TERMINAL_STATUSES = ("approved", "refused", "withdrawn")
CASE_EVENT_KINDS = (
    "opened",
    "stage_changed",
    "key_date_set",
    "document_requested",
    "document_uploaded",
    "document_verified",
    "document_rejected",
    "note",
    "outcome_recorded",
    "withdrawn",
    "permit_recorded",
)
RELATIONSHIPS = ("spouse", "child", "parent", "other")
DEPENDANT_VISA_STATUSES = ("not_started", "in_progress", "approved", "refused", "withdrawn")
COLLECT_FROM = ("worker", "counsel", "internal")
REQUEST_STATUSES = ("requested", "uploaded", "verified", "rejected", "waived")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ templates / visa types (310_01)
class VisaTypeTemplate(UUIDPk, Timestamps, Base):
    """Platform table (``core.tenancy.PLATFORM_TABLES``): no tenant, no RLS, read-only for the runtime role."""

    __tablename__ = "visa_type_templates"
    __table_args__ = (
        UniqueConstraint("key", name="uq_visa_type_templates_key"),
        CheckConstraint(_in("category", VISA_CATEGORIES), name="category"),
    )

    key: Mapped[str] = mapped_column(String(50), nullable=False)
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_reviewed: Mapped[date] = mapped_column(Date, nullable=False)
    notice: Mapped[str] = mapped_column(Text, nullable=False)


class VisaType(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "visa_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "key", name="uq_visa_types_key"),
        CheckConstraint(_in("category", VISA_CATEGORIES), name="category"),
    )

    key: Mapped[str] = mapped_column(String(50), nullable=False)
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reviewed: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VisaTypeVersion(UUIDPk, TenantMixin, Base):
    """Append-only (``enable_immutability(status_column=None)``)."""

    __tablename__ = "visa_type_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("visa_type_id", "version_no", name="uq_visa_type_versions_no"),
        _tenant_fk("visa_type_id", "visa_types", "fk_visa_type_versions_visa_type_id"),
        Index("ix_visa_type_versions_visa_type_id", "visa_type_id"),
    )

    visa_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ================================================================ counsel / cases (310_02)
class ImmigrationCounsel(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "immigration_counsel"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    firm: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImmigrationCase(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "immigration_cases"
    __pii_fields__ = ("outcome_note", "withdraw_reason")
    __pii_subject__ = "candidate"
    __audit_exclude__ = ("receipt_number",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_immigration_cases_human_id"),
        CheckConstraint(_in("status", CASE_STATUSES), name="status"),
        CheckConstraint(
            "receipt_number_last4 IS NULL OR length(receipt_number_last4) <= 4", name="receipt_last4"
        ),
        _tenant_fk("candidate_id", "candidates", "fk_immigration_cases_candidate_id"),
        _tenant_fk("visa_type_id", "visa_types", "fk_immigration_cases_visa_type_id"),
        _tenant_fk("visa_type_version_id", "visa_type_versions", "fk_immigration_cases_version_id"),
        _tenant_fk("placement_id", "placements", "fk_immigration_cases_placement_id"),
        _tenant_fk("project_id", "projects", "fk_immigration_cases_project_id"),
        _tenant_fk("demand_id", "demands", "fk_immigration_cases_demand_id"),
        _tenant_fk("owner_user_id", "users", "fk_immigration_cases_owner_user_id"),
        _tenant_fk("counsel_id", "immigration_counsel", "fk_immigration_cases_counsel_id"),
        _tenant_fk("sponsoring_legal_entity_id", "legal_entities", "fk_immigration_cases_sponsor_le_id"),
        Index("ix_immigration_cases_status_country", "status", "country"),
        Index("ix_immigration_cases_candidate_id", "candidate_id"),
        Index("ix_immigration_cases_owner_user_id", "owner_user_id"),
        Index("ix_immigration_cases_placement_id", "placement_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visa_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visa_type_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    stage_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    stage_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    counsel_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sponsoring_legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    key_dates: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    receipt_number: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    receipt_number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_actor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class CaseEvent(UUIDPk, TenantMixin, Base):
    """Append-only timeline (immutability trigger). ``payload`` never carries numbers or file contents."""

    __tablename__ = "case_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", CASE_EVENT_KINDS), name="kind"),
        _tenant_fk("case_id", "immigration_cases", "fk_case_events_case_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_case_events_candidate_id"),
        Index("ix_case_events_case_id", "case_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Copied from the Case: worker org RLS column (a Worker's portal upload writes its own timeline row).
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    from_stage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    to_stage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CaseDependant(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "case_dependants"
    __pii_fields__ = ("name", "notes")
    __pii_subject__ = "candidate"
    __audit_exclude__ = ("passport_number",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("relationship", RELATIONSHIPS), name="relationship"),
        CheckConstraint(_in("visa_status", DEPENDANT_VISA_STATUSES), name="visa_status"),
        CheckConstraint("passport_number_last4 IS NULL OR length(passport_number_last4) <= 4", name="last4"),
        _tenant_fk("case_id", "immigration_cases", "fk_case_dependants_case_id", "CASCADE"),
        Index("ix_case_dependants_case_id", "case_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship: Mapped[str] = mapped_column(String(20), nullable=False)
    nationality: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    passport_number: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    passport_number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    visa_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'not_started'"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ================================================================ document requests (310_03)
class CaseDocumentRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "case_document_requests"
    __pii_fields__ = ("rejection_reason",)
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("collect_from", COLLECT_FROM), name="collect_from"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        _tenant_fk("case_id", "immigration_cases", "fk_case_document_requests_case_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_case_document_requests_candidate_id"),
        _tenant_fk("dependant_id", "case_dependants", "fk_case_document_requests_dependant_id", "CASCADE"),
        Index("ix_case_document_requests_case_id", "case_id"),
        Index("ix_case_document_requests_candidate_id", "candidate_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    checklist_key: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    document_category: Mapped[str] = mapped_column(String(40), nullable=False)
    dependant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    collect_from: Mapped[str] = mapped_column(String(10), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'requested'"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)


# ================================================================ permits (310_04)
class Permit(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "permits"
    __pii_fields__ = ("conditions",)
    __pii_subject__ = "candidate"
    __audit_exclude__ = ("number", "number_bidx")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("number_last4 IS NULL OR length(number_last4) <= 4", name="last4"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="dates"),
        _tenant_fk("case_id", "immigration_cases", "fk_permits_case_id"),
        _tenant_fk("candidate_id", "candidates", "fk_permits_candidate_id"),
        _tenant_fk("visa_type_id", "visa_types", "fk_permits_visa_type_id"),
        Index("ix_permits_case_id", "case_id"),
        Index("ix_permits_candidate_id", "candidate_id"),
        Index("ix_permits_valid_to", "valid_to"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visa_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    permit_type: Mapped[str] = mapped_column(String(200), nullable=False)
    number: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    number_bidx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_values: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_actor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
