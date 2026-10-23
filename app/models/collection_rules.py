# ruff: file-ignore[line-too-long]
"""Collection rules, agent holds and the Collections Agent log (Epic 63, FR-314 / FR-315; migrations
``20261009_410_02_coll_rules`` and ``20261009_410_03_coll_log``). Owner: ``app.services.collection_rules``.

* ``collection_rules`` (audited) -- per-Client rule (``client_id`` NULL = the tenant default; at most one each):
  escalation Contact, weekly cap, quiet hours in the rule's time zone.
* ``collection_rule_steps`` (audited) -- schedule steps (offset from the due date, tone, audience, template). Only a
  templated pre-due courteous / friendly step to billing contacts may ever be sent automatically (CHECK ``auto``).
* ``collection_agent_holds`` (audited) -- a Finance User paused the agent or took over a thread for a Client or one
  document; one active hold per (client, document).
* ``collection_agent_log`` (append-only, not audited) -- every agent message, Proposal and decision for the Invoice /
  Client collection timeline. ``summary`` never carries amounts or Contact PII.

Every table: tenant RLS + internal-only org RLS (Phase 4 is not Client-visible).
"""

import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TONES = ("courteous", "friendly", "firm", "escalation")
AUDIENCES = ("billing_contacts", "escalation_contact", "both")
HOLD_MODES = ("paused", "takeover")
DOCUMENT_KINDS = ("invoice", "debit_note")
LOG_EVENTS = (
    "reminder_sent",
    "message_proposed",
    "proposal_approved",
    "proposal_edited_approved",
    "proposal_rejected",
    "proposal_expired",
    "proposal_stale",
    "message_sent",
    "held",
    "released",
    "takeover",
)
AUTO_CHECK = (
    "allow_auto = false OR (offset_days < 0 AND email_template_id IS NOT NULL "
    "AND audience = 'billing_contacts' AND tone IN ('courteous', 'friendly'))"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CollectionRule(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "collection_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "client_id", name="uq_collection_rules_client"),
        Index(
            "uq_collection_rules_default", "tenant_id", unique=True, postgresql_where=text("client_id IS NULL")
        ),
        _tfk("client_id", "clients", "fk_collection_rules_client_id", "CASCADE"),
        _tfk("escalation_contact_id", "contacts", "fk_collection_rules_escalation_contact_id"),
        CheckConstraint("max_reminders_per_week BETWEEN 1 AND 7", name="weekly"),
    )

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    escalation_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    max_reminders_per_week: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    quiet_start: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'18:00'"))
    quiet_end: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'09:00'"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class CollectionRuleStep(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "collection_rule_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "rule_id", "position", name="uq_collection_rule_steps_position"),
        _tfk("rule_id", "collection_rules", "fk_collection_rule_steps_rule_id", "CASCADE"),
        CheckConstraint("offset_days BETWEEN -30 AND 180", name="offset"),
        CheckConstraint(_in("tone", TONES), name="tone"),
        CheckConstraint(_in("audience", AUDIENCES), name="audience"),
        CheckConstraint(AUTO_CHECK, name="auto"),
        Index("ix_collection_rule_steps_rule_id", "rule_id"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    offset_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    tone: Mapped[str] = mapped_column(String(12), nullable=False)
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    email_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True
    )
    allow_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    create_task: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class CollectionAgentHold(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "collection_agent_holds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_collection_agent_holds_active",
            "tenant_id",
            "client_id",
            text("coalesce(document_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        _tfk("client_id", "clients", "fk_collection_agent_holds_client_id", "CASCADE"),
        CheckConstraint(_in("mode", HOLD_MODES), name="mode"),
        CheckConstraint(f"document_kind IS NULL OR {_in('document_kind', DOCUMENT_KINDS)}", name="doc_kind"),
        CheckConstraint("(document_id IS NULL) = (document_kind IS NULL)", name="doc_ref"),
        Index("ix_collection_agent_holds_client_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class CollectionAgentLog(UUIDPk, TenantMixin, Base):
    __tablename__ = "collection_agent_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tfk("client_id", "clients", "fk_collection_agent_log_client_id", "CASCADE"),
        CheckConstraint(_in("event", LOG_EVENTS), name="event"),
        Index("ix_collection_agent_log_client_at", "client_id", "at"),
        Index("ix_collection_agent_log_document_ids", "document_ids", postgresql_using="gin"),
        Index("ix_collection_agent_log_proposal_id", "proposal_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    event: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tone: Mapped[str | None] = mapped_column(String(12), nullable=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    collection_activity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


__all__ = [
    "AUDIENCES",
    "AUTO_CHECK",
    "DOCUMENT_KINDS",
    "HOLD_MODES",
    "LOG_EVENTS",
    "TONES",
    "CollectionAgentHold",
    "CollectionAgentLog",
    "CollectionRule",
    "CollectionRuleStep",
]
