# ruff: noqa: E501
"""Projects and project roles (Epic 36 hub tables, AD-39; owner Epic 45, branch ``p3_projects``).

Client org RLS on ``client_id``: a Client User session sees only its own client's projects and roles.
``project_roles.client_id`` is copied from the parent project by ``tos_inherit_org`` (never set by callers).
``projects.human_id`` = ``PRJ-nnnn`` (``core.sequences.next_human_id(db, "project", "PRJ")``).

Epic 45 (FR-227..FR-231) adds the hub detail columns (migration ``309_01``), ``project_assignments`` (309_01),
``deliverables`` / ``deliverable_events`` (309_02) and ``project_budgets`` / ``project_burn`` (309_03). The org
tables inherit ``client_id`` from their parent (``tos_inherit_org``); budgets and burn are internal only.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PROJECT_STATUSES = ("draft", "active", "on_hold", "completed", "cancelled")
PROJECT_ROLE_STATUSES = ("open", "filled", "closed", "cancelled")
# Epic 45
BILL_RATE_UNITS = ("hour", "day", "month", "fixed")
ASSIGNMENT_STATUSES = ("active", "ended")
DELIVERABLE_STATUSES = ("planned", "in_progress", "submitted", "accepted", "rejected", "cancelled")
DELIVERABLE_EVENT_KINDS = ("created", "started", "submitted", "accepted", "rejected", "cancelled", "reopened")
BUDGET_DIMENSIONS = ("hours", "cost", "revenue")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class Project(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        _tenant_fk("client_id", "clients", "fk_projects_client_id_clients"),
        _tenant_fk("contract_id", "contracts", "fk_projects_contract_id_contracts"),
        _tenant_fk("owner_id", "users", "fk_projects_owner_id_users"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_projects_legal_entity_id_legal_entities"),
        _tenant_fk("business_unit_id", "business_units", "fk_projects_business_unit_id_business_units"),
        CheckConstraint(_in("status", PROJECT_STATUSES), name="status"),
        CheckConstraint("end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="dates"),
        Index("ix_projects_client_id", "client_id"),
        Index("ix_projects_contract_id", "contract_id"),
        Index("ix_projects_owner_id", "owner_id"),
        Index("ix_projects_business_unit_id", "business_unit_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Epic 45 detail columns (309_01).
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRole(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "project_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("project_id", "projects", "fk_project_roles_project_id_projects"),
        CheckConstraint(_in("status", PROJECT_ROLE_STATUSES), name="status"),
        CheckConstraint("headcount >= 1", name="headcount"),
        CheckConstraint("end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="dates"),
        CheckConstraint(
            f"bill_rate_unit IS NULL OR {_in('bill_rate_unit', BILL_RATE_UNITS)}", name="rate_unit"
        ),
        CheckConstraint("bill_rate IS NULL OR bill_rate >= 0", name="bill_rate"),
        Index("ix_project_roles_project_id", "project_id"),
        Index("ix_project_roles_client_id", "client_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Inherited from the project (tos_inherit_org); callers never set it.
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Epic 45 detail columns (309_01). ``skills``: [{skill_key, label, min_proficiency}] (SkillKey.as_str()).
    skills: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    bill_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    bill_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectAssignment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A Placement staffed on a project role (Epic 45, FR-229). One active assignment per Placement (the
    placement link columns are scalar); ``candidate_id`` is denormalised and nulled by erasure."""

    __tablename__ = "project_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("project_id", "projects", "fk_project_assignments_project_id_projects"),
        _tenant_fk(
            "project_role_id", "project_roles", "fk_project_assignments_project_role_id_project_roles"
        ),
        _tenant_fk("placement_id", "placements", "fk_project_assignments_placement_id_placements"),
        CheckConstraint(_in("status", ASSIGNMENT_STATUSES), name="status"),
        CheckConstraint("allocation_pct > 0 AND allocation_pct <= 100", name="allocation"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
        Index("ix_project_assignments_project_id", "project_id"),
        Index("ix_project_assignments_project_role_id", "project_role_id"),
        Index("ix_project_assignments_placement_id", "placement_id"),
        Index("ix_project_assignments_client_id", "client_id"),
        Index(
            "uq_project_assignments_active_placement",
            "placement_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_role_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    allocation_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("100"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    ended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Deliverable(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A project deliverable (Epic 45, FR-228). ``rejected`` lives on the event only: a rejection returns the
    row to ``in_progress``. Acceptance is human-only (FR-257)."""

    __tablename__ = "deliverables"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("project_id", "projects", "fk_deliverables_project_id_projects"),
        _tenant_fk("milestone_id", "contract_milestones", "fk_deliverables_milestone_id_contract_milestones"),
        CheckConstraint(_in("status", DELIVERABLE_STATUSES), name="status"),
        Index("ix_deliverables_project_id", "project_id"),
        Index("ix_deliverables_milestone_id", "milestone_id"),
        Index("ix_deliverables_client_id", "client_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'planned'"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_actor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_file_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class DeliverableEvent(UUIDPk, TenantMixin, Base):
    """Append-only deliverable history (immutability trigger); the acceptance row stores the ActorRef."""

    __tablename__ = "deliverable_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("deliverable_id", "deliverables", "fk_deliverable_events_deliverable_id_deliverables"),
        CheckConstraint(_in("kind", DELIVERABLE_EVENT_KINDS), name="kind"),
        Index("ix_deliverable_events_deliverable_id", "deliverable_id"),
        Index("ix_deliverable_events_client_id", "client_id"),
        Index(
            "uq_deliverable_events_accepted",
            "deliverable_id",
            unique=True,
            postgresql_where=text("kind = 'accepted'"),
        ),
    )

    deliverable_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProjectBudget(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Budget per project and dimension (internal only). Editing the amount bumps ``version`` and clears the
    alert stamps, so each threshold alerts once per budget version."""

    __tablename__ = "project_budgets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("project_id", "dimension"),
        _tenant_fk("project_id", "projects", "fk_project_budgets_project_id_projects"),
        CheckConstraint(_in("dimension", BUDGET_DIMENSIONS), name="dimension"),
        CheckConstraint("amount >= 0", name="amount"),
        CheckConstraint("(dimension = 'hours') = (currency IS NULL)", name="currency"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    alert_80_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_100_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectBurn(UUIDPk, TenantMixin, Timestamps, Base):
    """Burn read model (one row per project and dimension), rebuilt from posted facts only (AD-33)."""

    __tablename__ = "project_burn"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("project_id", "dimension"),
        _tenant_fk("project_id", "projects", "fk_project_burn_project_id_projects"),
        CheckConstraint(_in("dimension", BUDGET_DIMENSIONS), name="dimension"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(String(10), nullable=False)
    actual: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    burn_rate_per_day: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    forecast_at_completion: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)


__all__ = [
    "ASSIGNMENT_STATUSES",
    "BILL_RATE_UNITS",
    "BUDGET_DIMENSIONS",
    "DELIVERABLE_EVENT_KINDS",
    "DELIVERABLE_STATUSES",
    "PROJECT_ROLE_STATUSES",
    "PROJECT_STATUSES",
    "Deliverable",
    "DeliverableEvent",
    "Project",
    "ProjectAssignment",
    "ProjectBudget",
    "ProjectBurn",
    "ProjectRole",
]
