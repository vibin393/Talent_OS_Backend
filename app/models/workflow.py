"""Workflow rules and their executions (Epic 10, story 10.4, FR-58/FR-59). Tenant-owned with forced RLS.
``workflow_executions`` is high-volume and NOT audited (rules are).

Migration: 20261001_103 (workflow).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

MODES = ("live", "dry_run")
EXECUTION_STATUSES = (
    "matched",
    "not_matched",
    "succeeded",
    "partially_failed",
    "failed",
    "skipped_rate_limit",
    "skipped_loop",
    # Epic 32: graph runs waiting on a delay / approval node, and runs cancelled with their rule.
    "waiting",
    "cancelled",
)
WAIT_KINDS = ("delay", "approval")
WAIT_STATUSES = ("waiting", "resumed", "cancelled", "timed_out")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _user_fk(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["users.tenant_id", "users.id"],
        name=f"fk_workflow_rules_{column}_users",
        ondelete="RESTRICT",
    )


class WorkflowRule(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "workflow_rules"
    __table_args__ = (
        _user_fk("created_by"),
        _user_fk("updated_by"),
        Index("ix_workflow_rules_trigger_active", "tenant_id", "trigger", "is_active"),
        CheckConstraint(
            "(definition_version = 1 AND graph IS NULL) OR (definition_version = 2 AND graph IS NOT NULL)",
            name="definition_version",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 32 (AD-31): NULL = Phase 1 form (run by the unchanged P1 engine); else a v2 graph.
    graph: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class WorkflowRuleVersion(UUIDPk, TenantMixin, Base):
    """Immutable copy of a rule's definition per version (Epic 32); runs and waits pin ``rule_version``."""

    __tablename__ = "workflow_rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version"),)

    rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_rules.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False, server_default=text("'[]'::jsonb"))
    graph: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowWait(UUIDPk, TenantMixin, Timestamps, Base):
    """A graph run parked on a delay or approval node (Epic 32); resumed by ``workflow.resume_waits`` or the
    approval decision (``workflow.continue_run`` after commit)."""

    __tablename__ = "workflow_waits"
    __table_args__ = (
        CheckConstraint(_in("kind", WAIT_KINDS), name="kind"),
        CheckConstraint(_in("status", WAIT_STATUSES), name="status"),
        CheckConstraint("kind <> 'delay' OR resume_at IS NOT NULL", name="delay_resume"),
        Index("ix_workflow_waits_status_resume", "tenant_id", "status", "resume_at"),
        Index("ix_workflow_waits_timeout", "timeout_at", postgresql_where=text("status = 'waiting'")),
        Index("ix_workflow_waits_rule", "rule_id", "status"),
        Index("ix_workflow_waits_execution", "execution_id"),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_rules.id", ondelete="CASCADE"), nullable=False
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    resume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'waiting'"))
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    continued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extras: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))


class WorkflowExecution(UUIDPk, TenantMixin, Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint("rule_id", "event_id", "entity_id", "mode"),
        CheckConstraint(_in("mode", MODES), name="mode"),
        CheckConstraint(_in("status", EXECUTION_STATUSES), name="status"),
        Index("ix_workflow_executions_rule_created", "tenant_id", "rule_id", text("created_at DESC")),
        Index("ix_workflow_executions_rule_entity", "rule_id", "entity_id", "created_at"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_rules.id", ondelete="CASCADE"), nullable=False
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'live'"))
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    condition_trace: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    # [{index, type, status ok|error|would_run, detail, error_code}]
    action_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    # Epic 32: graph runs, [{node_id, type, status, detail, at}]
    node_trace: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
