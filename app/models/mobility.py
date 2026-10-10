# ruff: noqa: E501
"""Internal mobility (Epic 41, branch ``p3_mobility``, migrations ``20261007_305_01..02``; FR-209..212, AD-50).

* ``mobility_settings`` -- one row per tenant (board switches, screening step, fallback approver role, default
  minimum tenure, disclosure text). Internal only.
* ``mobility_profiles`` -- optional grade / location of an Employee (``subject_kind`` user | worker, subject id =
  user id or candidate id); used only by eligibility. Internal only.
* ``mobility_employee_links`` -- the Candidate created / linked for an internal User on their first internal
  application (no new principal, AD-50). Internal only.
* ``internal_opening_rules`` -- optional eligibility rules per Demand. Internal only.
* ``internal_openings`` -- the board read projection (no client name, no rates / compensation, no hiring-manager
  contact). Custom RESTRICTIVE org policy: internal and worker sessions read it; client / vendor sessions see no
  rows.
* ``mobility_applications`` -- the overlay of a P1 Application with ``source='internal'`` (link =
  ``application_id`` UNIQUE + ``applications.source_ref``). Worker org RLS on ``candidate_id``.
* ``internal_transfers`` -- the confirmed transfer (305_02). Worker org RLS on ``candidate_id``.
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SUBJECT_KINDS = ("user", "worker")
EMPLOYEE_KINDS = ("user", "worker")
VISIBILITIES = ("internal", "both")
OPENING_STATUSES = ("open", "closed")
APPLICATION_STATUSES = (
    "applied",
    "manager_approval",
    "approved",
    "manager_rejected",
    "withdrawn",
    "transfer_confirmed",
)
# Overlay statuses that hold the Application at ``applied`` (pipeline transition guard).
HOLD_STATUSES = ("applied", "manager_approval", "manager_rejected", "withdrawn")
LIVE_STATUSES = ("applied", "manager_approval", "approved")
DEFAULT_DISCLOSURE = (
    "Your current manager will be asked to approve this move when your application reaches the approval step."
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ 305_01
class MobilitySettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "mobility_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(
            "min_tenure_default_months IS NULL OR min_tenure_default_months BETWEEN 0 AND 600", name="tenure"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    board_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    worker_board_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    screen_before_manager: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fallback_approver_role_key: Mapped[str] = mapped_column(
        String(60), nullable=False, server_default=text("'recruitment_manager'")
    )
    min_tenure_default_months: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    disclosure_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text(f"'{DEFAULT_DISCLOSURE}'"))


class MobilityProfile(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "mobility_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "subject_kind", "subject_id", name="uq_mobility_profiles_subject"),
        CheckConstraint(_in("subject_kind", SUBJECT_KINDS), name="subject_kind"),
        _tenant_fk("updated_by", "users", "fk_mobility_profiles_updated_by_users"),
    )

    subject_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    grade: Mapped[str | None] = mapped_column(String(40), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class MobilityEmployeeLink(UUIDPk, TenantMixin, AuditedMixin, Base):
    __tablename__ = "mobility_employee_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "user_id", name="uq_mobility_employee_links_user"),
        UniqueConstraint("tenant_id", "candidate_id", name="uq_mobility_employee_links_cand"),
        _tenant_fk("user_id", "users", "fk_mobility_employee_links_user_id"),
        _tenant_fk("candidate_id", "candidates", "fk_mobility_employee_links_candidate_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class InternalOpeningRule(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "internal_opening_rules"
    __audit_entity_id__ = "demand_id"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_internal_opening_rules_demand_id", "CASCADE"),
        CheckConstraint("min_tenure_months IS NULL OR min_tenure_months BETWEEN 0 AND 600", name="tenure"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    min_tenure_months: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    grades: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    locations: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    business_unit_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class InternalOpening(TenantMixin, Timestamps, Base):
    """Read projection (maintained by handlers + nightly reconcile; not audited -- derived data)."""

    __tablename__ = "internal_openings"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_internal_openings_demand_id", "CASCADE"),
        CheckConstraint(_in("visibility", VISIBILITIES), name="visibility"),
        CheckConstraint(_in("status", OPENING_STATUSES), name="status"),
        Index("ix_internal_openings_status", "tenant_id", "status"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    location_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    summary_html: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))


class MobilityApplication(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "mobility_applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("application_id", name="uq_mobility_applications_application_id"),
        CheckConstraint(_in("employee_kind", EMPLOYEE_KINDS), name="employee_kind"),
        CheckConstraint(_in("status", APPLICATION_STATUSES), name="status"),
        CheckConstraint("employee_kind = 'worker' OR user_id IS NOT NULL", name="user_link"),
        _tenant_fk("application_id", "applications", "fk_mobility_applications_application_id"),
        _tenant_fk("demand_id", "demands", "fk_mobility_applications_demand_id"),
        _tenant_fk("candidate_id", "candidates", "fk_mobility_applications_candidate_id"),
        _tenant_fk("user_id", "users", "fk_mobility_applications_user_id"),
        _tenant_fk("current_placement_id", "placements", "fk_mobility_applications_placement_id"),
        _tenant_fk("manager_user_id", "users", "fk_mobility_applications_manager_user_id"),
        Index("ix_mobility_applications_demand_id", "demand_id"),
        Index("ix_mobility_applications_candidate_id", "candidate_id"),
        Index("ix_mobility_applications_user_id", "user_id"),
    )
    __audit_exclude__ = ("cover_note",)

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    employee_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'applied'"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ 305_02
class InternalTransfer(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "internal_transfers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("mobility_application_id", name="uq_internal_transfers_mobility_app"),
        CheckConstraint(_in("employee_kind", EMPLOYEE_KINDS), name="employee_kind"),
        _tenant_fk("mobility_application_id", "mobility_applications", "fk_internal_transfers_mobility_app"),
        _tenant_fk("candidate_id", "candidates", "fk_internal_transfers_candidate_id"),
        _tenant_fk("user_id", "users", "fk_internal_transfers_user_id"),
        _tenant_fk("from_placement_id", "placements", "fk_internal_transfers_from_placement"),
        _tenant_fk("to_placement_id", "placements", "fk_internal_transfers_to_placement"),
        _tenant_fk("from_team_id", "teams", "fk_internal_transfers_from_team_id"),
        _tenant_fk("to_team_id", "teams", "fk_internal_transfers_to_team_id"),
        _tenant_fk("confirmed_by", "users", "fk_internal_transfers_confirmed_by"),
        Index("ix_internal_transfers_candidate_id", "candidate_id"),
    )

    mobility_application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    employee_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    from_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_role_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confirmed_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
