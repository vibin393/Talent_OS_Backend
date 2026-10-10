# ruff: noqa: E501
"""Compliance requirements and credentials (Epic 21, branch ``p2_compliance``, migrations ``20261005_208_01..03``).

* Configuration (internal only): ``credential_types``, ``compliance_requirements``, ``compliance_settings``.
* ``credentials``: versioned, numbers are ``EncryptedStr`` + ``number_last4`` / ``number_bidx`` (AD-35); an
  Aadhaar credential only ever has ``number_last4`` (NFR-24). Worker org RLS on ``candidate_id``.
* ``credential_alerts`` (one row per credential and offset = dedupe), ``compliance_recompute_runs`` (one per
  local date), ``placement_compliance_items`` (the per-Placement checklist), ``compliance_evaluation_runs``.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CREDENTIAL_CATEGORIES = (
    "passport",
    "visa_work_permit",
    "certification",
    "professional_licence",
    "insurance",
    "client_document",
    "identity",
    "other",
)
NUMBER_KINDS = ("none", "encrypted", "aadhaar_last4")
ITEM_KINDS = ("credential", "document", "bgv_package", "policy_ack")
ITEM_STATUSES = ("missing", "pending", "met", "expiring", "expired", "waived")
VERIFICATION_STATUSES = ("pending_verification", "valid", "rejected")
EXPIRY_STATUSES = ("none", "expiring_soon", "expired")
REDACTION_STATUSES = ("pending_review", "confirmed", "rejected")
CREDENTIAL_SOURCES = ("user", "worker_portal", "onboarding", "bgv", "import")
EVALUATE_AT = ("placement_created", "application_selected")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CredentialType(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "credential_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "key", name="uq_credential_types_key"),
        CheckConstraint(_in("category", CREDENTIAL_CATEGORIES), name="category"),
        CheckConstraint(_in("number_kind", NUMBER_KINDS), name="number_kind"),
    )

    key: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    has_expiry: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    number_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    requires_document: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    alert_offsets: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComplianceRequirement(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "compliance_requirements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("client_id", "clients", "fk_compliance_requirements_client_id_clients"),
        Index("ix_compliance_requirements_client_id", "client_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # type: ignore[type-arg]
    items: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # type: ignore[type-arg]
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComplianceSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "compliance_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_compliance_settings_tenant_id"),
        CheckConstraint(_in("evaluate_at", EVALUATE_AT), name="evaluate_at"),
        CheckConstraint("recompute_hour_local BETWEEN 0 AND 23", name="recompute_hour_local"),
    )

    evaluate_at: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'placement_created'")
    )
    default_alert_offsets: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{60,30,7}'::integer[]")
    )
    recompute_hour_local: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))


class Credential(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "credentials"
    __pii_fields__ = ("issuer", "rejection_note")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __audit_exclude__ = ("number", "number_bidx")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_credentials_human_id"),
        CheckConstraint(_in("verification_status", VERIFICATION_STATUSES), name="verification_status"),
        CheckConstraint(_in("expiry_status", EXPIRY_STATUSES), name="expiry_status"),
        CheckConstraint(
            f"redaction_status IS NULL OR {_in('redaction_status', REDACTION_STATUSES)}", name="redaction"
        ),
        CheckConstraint(_in("source", CREDENTIAL_SOURCES), name="source"),
        CheckConstraint("number_last4 IS NULL OR length(number_last4) <= 4", name="last4_len"),
        _tenant_fk("candidate_id", "candidates", "fk_credentials_candidate_id_candidates"),
        _tenant_fk("credential_type_id", "credential_types", "fk_credentials_credential_type_id"),
        _tenant_fk("previous_id", "credentials", "fk_credentials_previous_id"),
        Index("ix_credentials_candidate_id", "candidate_id"),
        Index("ix_credentials_type_key", "type_key"),
        Index("ix_credentials_expires_on", "expires_on"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    credential_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type_key: Mapped[str] = mapped_column(String(60), nullable=False)
    number: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    number_bidx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    redaction_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(25), nullable=False, server_default=text("'pending_verification'")
    )
    expiry_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    previous_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'user'"))
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CredentialAlert(UUIDPk, TenantMixin, Base):
    __tablename__ = "credential_alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "credential_id", "offset_days", name="uq_credential_alerts_key"),
        _tenant_fk("credential_id", "credentials", "fk_credential_alerts_credential_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_credential_alerts_candidate_id_candidates"),
    )

    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # -1 = the "expired" alert
    offset_days: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ComplianceRecomputeRun(UUIDPk, TenantMixin, Base):
    __tablename__ = "compliance_recompute_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_date", name="uq_compliance_recompute_runs_run_date"),
    )

    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # type: ignore[type-arg]


class PlacementComplianceItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "placement_compliance_items"
    __pii_fields__ = ("waived_reason",)
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("placement_id", "kind", "ref", name="uq_placement_compliance_items_key"),
        CheckConstraint(_in("kind", ITEM_KINDS), name="kind"),
        CheckConstraint(_in("status", ITEM_STATUSES), name="status"),
        _tenant_fk("placement_id", "placements", "fk_placement_compliance_items_placement_id", "CASCADE"),
        Index("ix_placement_compliance_items_placement_id", "placement_id"),
        Index("ix_placement_compliance_items_candidate_id", "candidate_id"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Inherited from the placement (org RLS trigger); the worker reads labels of their own checklist.
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requirement_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    ref: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'missing'"))
    retroactive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    waived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    waived_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComplianceEvaluationRun(UUIDPk, TenantMixin, Base):
    __tablename__ = "compliance_evaluation_runs"

    requirement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    placements_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    newly_non_compliant: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
