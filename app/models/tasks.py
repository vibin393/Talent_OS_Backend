"""Tasks (Epic 10, story 10.2, FR-56): follow-ups with owner, due date, priority and reminder, optionally
linked to a client, contact, demand, candidate or application. Tenant-owned with forced RLS.

Migration: 20261001_102 (tasks and notifications).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TASK_ENTITY_TYPES = (
    "client", "contact", "demand", "candidate", "application", "lead", "opportunity",  # Epic 15: +lead, opportunity
    # Epic 36 (300_01): workflow "create task" for Phase 3 subjects.
    "offer", "placement", "project", "deliverable", "immigration_case", "campus_drive", "workforce_plan",
    "referral", "talent_pool", "campaign",
    # Epic 53 (400_01): agent fallback Tasks link the run / the Proposal.
    "agent_run", "agent_proposal",
    # Epic 66 (500_01): every Phase 5 fix-it Task subject (no later epic widens this CHECK).
    "installation", "webhook_subscription", "board_posting", "hrms_sync", "ledger_sync", "payment_suggestion",
    "calendar_change", "network_listing", "mcp_session",
)  # fmt: skip
PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("open", "done", "cancelled")
SOURCES = (
    "manual", "workflow",
    # Epic 13 (R7): system tasks created by Phase 2 handlers / beats / portal sessions (``created_by`` NULL).
    "onboarding", "offboarding", "collections", "client_portal", "bgv",
    # Epic 53 (400_01): agent tasks (core tool ``tasks.create`` and run fallbacks); the agent key is in the title/notes.
    "agent",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _user_fk(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["users.tenant_id", "users.id"],
        name=f"fk_tasks_{column}_users",
        ondelete="RESTRICT",
    )


class Task(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tasks"
    __pii_fields__ = ("title", "notes")
    __table_args__ = (
        _user_fk("owner_id"),
        _user_fk("created_by"),
        CheckConstraint(
            f"entity_type IS NULL OR {_in('entity_type', TASK_ENTITY_TYPES)}", name="entity_type"
        ),
        CheckConstraint("(entity_type IS NULL) = (entity_id IS NULL)", name="entity_pair"),
        CheckConstraint(_in("priority", PRIORITIES), name="priority"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        Index("ix_tasks_owner_status_due", "tenant_id", "owner_id", "status", "due_at"),
        Index("ix_tasks_entity", "tenant_id", "entity_type", "entity_id"),
        Index(
            "ix_tasks_reminder_due",
            "tenant_id",
            "reminder_at",
            postgresql_where=text("status = 'open' AND reminded_at IS NULL"),
        ),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # NULL for system tasks (Epic 13, R7: ``tasks_service.create_system_task``).
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    priority: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'normal'"))
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'manual'"))
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
