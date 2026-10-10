"""Generic approval chains (Epic 4, story 4.2; reused by offers and margin override; extended by Epic 13).

Tenant-owned with forced RLS (migration 20261001_42). A request snapshots its chain's steps when it starts,
so editing a chain never changes requests already in flight.

Epic 13 (AD-24, migration 20261002_200_04): subject types are registry-validated (no CHECK); steps carry an
``approver_kind`` + ``approver_ref`` resolved at snapshot; requests may carry explicit steps; decisions record
the full ``ActorRef`` (kind, attribution, delegation, offline evidence); escalation policies are stored rows
(evaluated by Epic 32). Org RLS: requests / decisions are visible to an external org only through
``org_grants`` (``approval_request``); chains, steps and escalation policies are internal-only.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

# Phase 1 subject types (kept for the chain settings API). Since Epic 13 the column is registry-validated.
SUBJECT_TYPES = ("demand", "offer", "margin_override")
STEPS_SOURCES = ("chain", "explicit")
# Epic 32 widens the owner kinds with ``library`` (reusable policies, ``owner_ref`` NULL).
ESCALATION_OWNER_KINDS = ("approval_chain_step", "workflow_node", "task_type", "library")
ESCALATION_AFTER_UNITS = ("business_hours", "business_days", "hours")
ESCALATION_ACTIONS = ("notify", "reassign")
ESCALATION_TARGET_KINDS = ("user", "manager_of_assignee", "role")
REQUEST_STATUSES = ("pending", "approved", "rejected", "changes_requested", "cancelled")
DECISION_OUTCOMES = ("approved", "rejected", "changes_requested", "skipped")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ApprovalChain(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "approval_chains"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_approval_chains_active_subject",
            "tenant_id",
            "subject_type",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ApprovalChainStep(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "approval_chain_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "chain_id"],
            ["approval_chains.tenant_id", "approval_chains.id"],
            name="fk_approval_chain_steps_chain_id_approval_chains",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approver_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_approval_chain_steps_approver_user_id_users",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approver_role_id"],
            ["roles.tenant_id", "roles.id"],
            name="fk_approval_chain_steps_approver_role_id_roles",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("chain_id", "position"),
        CheckConstraint(
            "(approver_kind = 'user' AND approver_user_id IS NOT NULL) "
            "OR (approver_kind = 'role' AND approver_role_id IS NOT NULL) "
            "OR approver_kind NOT IN ('user', 'role')",
            name="one_approver",
        ),
        CheckConstraint("position >= 1", name="position"),
    )

    chain_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approver_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    condition: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    approver_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    approver_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)


class ApprovalRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """``current_approver_*`` denormalise the current step of ``chain_snapshot`` (for "My approvals").
    ``chain_id`` is informational (no FK): the snapshot carries everything, and chains may be deleted."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint(_in("steps_source", STEPS_SOURCES), name="steps_source"),
        Index("ix_approval_requests_subject", "subject_type", "subject_id"),
        Index("ix_approval_requests_pending_user", "current_approver_user_id", "status"),
        Index("ix_approval_requests_pending_role", "current_approver_role_id", "status"),
        Index("ix_approval_requests_pending_client_user", "current_approver_client_user_id", "status"),
    )
    __audit_exclude__ = ("chain_snapshot", "facts")

    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    chain_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    current_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_approver_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 13 (AD-24)
    steps_source: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'chain'"))
    current_approver_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_approver_client_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_step_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    initiated_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)


class ApprovalDecision(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["approval_requests.tenant_id", "approval_requests.id"],
            name="fk_approval_decisions_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("outcome", DECISION_OUTCOMES), name="outcome"),
        Index("ix_approval_decisions_request_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_approval_decisions_evidence_file_id_stored_files",
            ondelete="RESTRICT",
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(200), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_condition: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Epic 13 (AD-24): the full ActorRef. ``actor_id`` = the User for ``user`` / ``recorded_by_user``, else the
    # principal or link id.
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'user'"))
    attributed_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attributed_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class EscalationPolicy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """An escalation rule owned by an approval chain step, workflow node or task type (stored only; Epic 32
    evaluates them)."""

    __tablename__ = "escalation_policies"
    __table_args__ = (
        CheckConstraint(_in("owner_kind", ESCALATION_OWNER_KINDS), name="owner_kind"),
        CheckConstraint(_in("action", ESCALATION_ACTIONS), name="action"),
        CheckConstraint(_in("target_kind", ESCALATION_TARGET_KINDS), name="target_kind"),
        CheckConstraint("after_business_hours > 0", name="after_business_hours"),
        CheckConstraint("repeat_every_hours IS NULL OR repeat_every_hours > 0", name="repeat_every_hours"),
        CheckConstraint("max_repeats >= 0", name="max_repeats"),
        Index("ix_escalation_policies_owner", "owner_kind", "owner_ref"),
        # Epic 32 (migration 20261003_219_02)
        CheckConstraint("(owner_kind = 'library') = (owner_ref IS NULL)", name="owner_ref"),
        CheckConstraint(_in("after_unit", ESCALATION_AFTER_UNITS), name="after_unit"),
        CheckConstraint(_in("calendar", ("business", "calendar")), name="calendar"),
        CheckConstraint("target_kind = 'manager_of_assignee' OR target_ref IS NOT NULL", name="target_ref"),
        Index("ix_escalation_policies_source", "source_policy_id"),
    )

    owner_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    after_business_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    repeat_every_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    max_repeats: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    # Epic 32: library / display fields (``after_unit`` is the display unit of ``after_business_hours``).
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    after_unit: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'business_hours'"))
    calendar: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'business'"))
    notify_original_assignee: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True
    )
