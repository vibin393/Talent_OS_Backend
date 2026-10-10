"""Placement hub tables (Epic 13 contract layer, AD-25, AD-36) plus the Epic 19 tables (branch ``p2_placements``,
migrations ``20261004_206_01..03``).

* ``placements``: ``client_id`` / ``vendor_id`` / ``candidate_id`` are immutable (trigger) and drive org RLS
  (client, vendor and worker sessions see only their own placements). Epic 19 adds engagement, joining, dropout
  and cache columns; ``status`` is written only by ``placements.status.transition_placement`` (AD-4).
* ``rate_segments``: effective-dated, append-only (only ``effective_to`` / ``status`` / ``approval_request_id``
  may change), non-overlapping among *active* segments (EXCLUDE ... WHERE status='active').
* ``start_gates`` / ``start_gate_overrides`` / ``placement_touchpoints`` / ``placement_status_history``.
* ``worker_profiles`` / ``worker_bank_accounts``: PAN and bank numbers are ``EncryptedStr`` (AD-35).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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

WORKER_TYPES = ("employee", "contractor", "vendor_contractor")
PAYEE_KINDS = ("worker", "vendor")
PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active", "offboarding", "ended", "dropped")
RATE_UNITS = ("hour", "day", "month", "fixed")
# Epic 19
ENGAGEMENT_KINDS = ("contract", "c2h", "permanent")
JOINING_VIA = ("portal", "secure_link", "user")
DROP_REASONS = ("declined", "no_show", "bgv_failed", "client_cancelled")
SEGMENT_STATUSES = ("pending_approval", "active", "rejected")
GATE_STATUSES = ("pending", "met", "failed", "waived", "not_required")
OVERRIDE_STATUSES = ("pending", "approved", "rejected")
TOUCHPOINT_RESPONSES = ("confirmed", "question")
PAN_STATUSES = ("unverified", "hr_checked")
BANK_STATUSES = ("pending_approval", "active", "superseded", "rejected")
BANK_SOURCES = ("onboarding", "worker_portal", "user", "import")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class Placement(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "placements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "offer_id"),
        CheckConstraint(_in("worker_type", WORKER_TYPES), name="worker_type"),
        CheckConstraint(_in("payee_kind", PAYEE_KINDS), name="payee_kind"),
        CheckConstraint(_in("status", PLACEMENT_STATUSES), name="status"),
        CheckConstraint("(payee_kind = 'vendor') = (vendor_id IS NOT NULL)", name="vendor_payee"),
        _tenant_fk("candidate_id", "candidates", "fk_placements_candidate_id_candidates"),
        _tenant_fk("client_id", "clients", "fk_placements_client_id_clients"),
        _tenant_fk("vendor_id", "vendors", "fk_placements_vendor_id_vendors"),
        _tenant_fk("application_id", "applications", "fk_placements_application_id_applications"),
        _tenant_fk("offer_id", "offers", "fk_placements_offer_id_offers"),
        _tenant_fk("demand_id", "demands", "fk_placements_demand_id_demands"),
        _tenant_fk("contract_id", "contracts", "fk_placements_contract_id_contracts"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_placements_legal_entity_id_legal_entities"),
        _tenant_fk(
            "supplier_gst_registration_id",
            "gst_registrations",
            "fk_placements_supplier_gst_registration_id_gst_registrations",
        ),
        _tenant_fk("account_manager_user_id", "users", "fk_placements_account_manager_user_id_users"),
        _tenant_fk("recruiter_user_id", "users", "fk_placements_recruiter_user_id_users"),
        Index("ix_placements_candidate_id", "candidate_id"),
        Index("ix_placements_client_id", "client_id"),
        Index("ix_placements_vendor_id", "vendor_id"),
        Index("ix_placements_demand_id", "demand_id"),
        Index("ix_placements_legal_entity_id", "legal_entity_id"),
        # Epic 36 (300_03): project staffing links, written only via placements_service.link_project.
        _tenant_fk("project_id", "projects", "fk_placements_project_id_projects"),
        _tenant_fk("project_role_id", "project_roles", "fk_placements_project_role_id_project_roles"),
        CheckConstraint("project_role_id IS NULL OR project_id IS NOT NULL", name="project_role_project"),
        Index("ix_placements_project_id", "project_id"),
        Index("ix_placements_project_role_id", "project_role_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    supplier_gst_registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 19 (206_01): NULL only for permanent placements.
    worker_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payee_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pre_joining'"))
    planned_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    work_state_code: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("gst_states.code", ondelete="RESTRICT"), nullable=True
    )
    work_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Asia/Kolkata'")
    )
    account_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recruiter_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # ---- Epic 19 (206_01)
    engagement_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    po_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rate_card_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    holiday_calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_route_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    work_location_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_manager_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    joining_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joining_confirmed_via: Mapped[str | None] = mapped_column(String(20), nullable=True)
    drop_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    drop_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    dropout_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    dropout_risk_rules: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    dropout_cleared_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dropout_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counter_offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counter_offer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    setup_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    onboarding_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bgv_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    compliance_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    non_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    gate_alert_sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # ---- Epic 36 (300_03)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    project_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class RateSegment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The EXCLUDE constraint ``ex_rate_segments_no_overlap`` (gist, ``daterange(effective_from, effective_to,
    '[]')`` among ``status='active'`` rows; NULL upper = open-ended) is created by the migration."""

    __tablename__ = "rate_segments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("rate_unit", RATE_UNITS), name="rate_unit"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="dates"),
        CheckConstraint(
            "(pay_rate IS NULL OR pay_rate >= 0) AND (bill_rate IS NULL OR bill_rate >= 0) "
            "AND (vendor_rate IS NULL OR vendor_rate >= 0)",
            name="rates",
        ),
        _tenant_fk("placement_id", "placements", "fk_rate_segments_placement_id_placements"),
        Index("ix_rate_segments_placement_id", "placement_id", "effective_from"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    rate_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    pay_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    bill_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    vendor_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # ---- Epic 19 (206_01)
    burden_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    vendor_burden_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    margin_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    rate_check: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ================================================================ Epic 19 tables
class _Inherited:
    """Org columns copied from the placement by ``tos_inherit_org`` (org RLS ``inherit_from='placements'``)."""

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class PlacementStatusHistory(UUIDPk, TenantMixin, _Inherited, Base):
    __tablename__ = "placement_status_history"
    __table_args__ = (
        _tenant_fk(
            "placement_id", "placements", "fk_placement_status_history_placement_id_placements", "CASCADE"
        ),
        Index("ix_placement_status_history_placement_id", "placement_id", "at"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PlacementSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily (``GET/PATCH /placements/settings``)."""

    __tablename__ = "placement_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    human_id_prefix: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'PLC'"))
    touchpoint_offsets: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{14,7,3,1}'::int[]")
    )
    gate_alert_offset_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    ending_soon_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    worker_type_by_employment_type: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dropout_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    touchpoint_email_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)


class StartGate(UUIDPk, TenantMixin, Timestamps, _Inherited, Base):
    __tablename__ = "start_gates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "placement_id", "kind", "source_ref", name="uq_start_gates_key"),
        CheckConstraint(_in("status", GATE_STATUSES), name="status"),
        _tenant_fk("placement_id", "placements", "fk_start_gates_placement_id_placements", "CASCADE"),
        Index("ix_start_gates_placement_id", "placement_id"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    override_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StartGateOverride(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "start_gate_overrides"
    __table_args__ = (
        CheckConstraint(_in("status", OVERRIDE_STATUSES), name="status"),
        _tenant_fk(
            "placement_id", "placements", "fk_start_gate_overrides_placement_id_placements", "CASCADE"
        ),
        Index("ix_start_gate_overrides_placement_id", "placement_id"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gate_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlacementTouchpoint(UUIDPk, TenantMixin, Timestamps, _Inherited, Base):
    __tablename__ = "placement_touchpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "placement_id", "offset_days", name="uq_placement_touchpoints_offset"),
        CheckConstraint(f"response IS NULL OR {_in('response', TOUCHPOINT_RESPONSES)}", name="response"),
        _tenant_fk(
            "placement_id", "placements", "fk_placement_touchpoints_placement_id_placements", "CASCADE"
        ),
        Index("ix_placement_touchpoints_placement_id", "placement_id"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    offset_days: Mapped[int] = mapped_column(Integer, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response: Mapped[str | None] = mapped_column(String(20), nullable=True)
    response_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkerProfile(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "worker_profiles"
    __pii_fields__ = ("emergency_name", "emergency_relation", "emergency_phone", "pan_name")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __audit_exclude__ = ("pan", "pan_bidx")
    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", name="uq_worker_profiles_candidate_id"),
        UniqueConstraint("tenant_id", "worker_human_id", name="uq_worker_profiles_worker_human_id"),
        CheckConstraint(_in("pan_status", PAN_STATUSES), name="pan_status"),
        _tenant_fk("candidate_id", "candidates", "fk_worker_profiles_candidate_id_candidates"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    worker_human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    default_worker_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    emergency_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    emergency_relation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    emergency_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pan: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    pan_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pan_bidx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pan_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pan_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'unverified'"))


class WorkerBankAccount(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "worker_bank_accounts"
    __pii_fields__ = ("holder_name",)
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __audit_exclude__ = ("account_number", "account_number_bidx")
    __table_args__ = (
        CheckConstraint(_in("status", BANK_STATUSES), name="status"),
        CheckConstraint(_in("source", BANK_SOURCES), name="source"),
        _tenant_fk("candidate_id", "candidates", "fk_worker_bank_accounts_candidate_id_candidates"),
        Index(
            "uq_worker_bank_accounts_active",
            "tenant_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_worker_bank_accounts_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    holder_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_number: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    account_number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    account_number_bidx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending_approval'"))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ Epic 24 (bench management, branch ``p2_bench``)
BENCH_STATUSES = ("on_bench", "proposed", "redeployed", "released")
BENCH_OPEN_STATUSES = ("on_bench", "proposed")
BENCH_ORIGINS = ("placement_ended", "contractor_opt_in", "manual")
BENCH_COST_SOURCES = ("rate_segment", "manual")
BENCH_RELEASE_REASONS = ("resigned", "terminated", "contractor_unavailable", "other")
BENCH_DAY_BASES = ("calendar", "weekdays")
BENCH_REVIEW_DECISIONS = ("reviewed", "dismissed")


class BenchSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (lazily created by ``bench.settings_row``)."""

    __tablename__ = "bench_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (CheckConstraint(_in("bench_day_basis", BENCH_DAY_BASES), name="day_basis"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    hours_per_day: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, server_default=text("8.00"))
    monthly_rate_divisor: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, server_default=text("22.00")
    )
    bench_day_basis: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'weekdays'"))
    upcoming_roll_off_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    notify_strong_bench_match: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    require_bench_review_for_vendor_release: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    contractor_opt_in_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class BenchEntry(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A Worker between placements (PRD §5.14). ``status`` is written only by ``bench.transition_entry`` (AD-4)."""

    __tablename__ = "bench_entries"
    __pii_fields__ = ("notes", "released_note", "work_authorization")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __table_args__ = (
        CheckConstraint(_in("status", BENCH_STATUSES), name="status"),
        CheckConstraint(_in("origin", BENCH_ORIGINS), name="origin"),
        CheckConstraint(f"cost_source IS NULL OR {_in('cost_source', BENCH_COST_SOURCES)}", name="cost_source"),
        CheckConstraint(
            f"released_reason IS NULL OR {_in('released_reason', BENCH_RELEASE_REASONS)}", name="released_reason"
        ),
        CheckConstraint("cost_per_day IS NULL OR cost_per_day >= 0", name="cost_non_negative"),
        _tenant_fk("candidate_id", "candidates", "fk_bench_entries_candidate_id_candidates"),
        _tenant_fk("source_placement_id", "placements", "fk_bench_entries_source_placement_id_placements"),
        _tenant_fk(
            "redeployed_placement_id", "placements", "fk_bench_entries_redeployed_placement_id_placements"
        ),
        Index(
            "uq_bench_entries_open",
            "tenant_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("status IN ('on_bench', 'proposed')"),
        ),
        Index("ix_bench_entries_tenant_id_status", "tenant_id", "status"),
        Index("ix_bench_entries_tenant_id_candidate_id", "tenant_id", "candidate_id"),
        Index("ix_bench_entries_source_placement_id", "source_placement_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'on_bench'"))
    available_from: Mapped[date] = mapped_column(Date, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    skills_snapshot: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    work_authorization: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_locations: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    cost_per_day: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    cost_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cost_rate_unit_basis: Mapped[str | None] = mapped_column(String(60), nullable=True)
    proposed_application_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    redeployed_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    released_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    released_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class BenchMatchReview(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "bench_match_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "demand_id", "candidate_id", name="uq_bench_match_reviews_pair"),
        CheckConstraint(_in("decision", BENCH_REVIEW_DECISIONS), name="decision"),
        CheckConstraint("decision <> 'dismissed' OR reason IS NOT NULL", name="dismiss_reason"),
        _tenant_fk("candidate_id", "candidates", "fk_bench_match_reviews_candidate_id_candidates", "CASCADE"),
        _tenant_fk("demand_id", "demands", "fk_bench_match_reviews_demand_id_demands", "CASCADE"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BenchMatchNotice(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "bench_match_notices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "demand_id", "candidate_id", name="uq_bench_match_notices_pair"),
        _tenant_fk("candidate_id", "candidates", "fk_bench_match_notices_candidate_id_candidates", "CASCADE"),
        _tenant_fk("demand_id", "demands", "fk_bench_match_notices_demand_id_demands", "CASCADE"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    overall: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)


class BenchMatchReviewCompletion(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "bench_match_review_completions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "demand_id", name="uq_bench_match_review_completions_demand"),
        _tenant_fk("demand_id", "demands", "fk_bench_match_review_completions_demand_id_demands", "CASCADE"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
