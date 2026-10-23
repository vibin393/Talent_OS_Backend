# ruff: file-ignore[line-too-long]
"""Portal Copilot tables (Epic 77, AD-89; migration ``20261010_511_01_portal_copilot``). Owner: ``portal_copilot``.

* ``portal_copilot_conversations`` -- one private conversation of one portal principal (Client User, Vendor User or
  Candidate); exactly one org column (``client_id`` / ``vendor_id`` / ``candidate_id``) is set, so org RLS shows a
  portal session only its own organization's rows. ``title_sealed`` is sealed free text.
* ``portal_copilot_messages`` -- questions, answers and draft events; ``content_sealed`` =
  ``vault_service.seal_field(tenant, "portal_copilot_messages.content:<id>", json)``.
* ``portal_copilot_feedback`` -- thumbs up / down per run (comment sealed).
* ``portal_copilot_settings`` -- one row per tenant (enablement per portal, rate limits, retention, hand-off role).

Content tables are not audited (audit rows would copy private content); lifecycle actions are audited explicitly
without content.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PRINCIPAL_TYPES = ("client_user", "vendor_user", "candidate")
PORTAL_AGENT_KEYS = ("copilot_client", "copilot_vendor", "copilot_candidate")
CONVERSATION_STATUSES = ("open", "closed")
MESSAGE_ROLES = ("user", "assistant", "event")
RATINGS = ("up", "down")
FEEDBACK_REASONS = ("wrong", "not_grounded", "unhelpful", "unsafe", "other")
DEFAULT_QPH_EXTERNAL = 30
DEFAULT_QPH_CANDIDATE = 15
DEFAULT_RETENTION_DAYS = 30

_ONE_ORG = (
    "(principal_type = 'client_user') = (client_id IS NOT NULL) AND "
    "(principal_type = 'vendor_user') = (vendor_id IS NOT NULL) AND "
    "(principal_type = 'candidate') = (candidate_id IS NOT NULL)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _conv_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "conversation_id"],
        ["portal_copilot_conversations.tenant_id", "portal_copilot_conversations.id"],
        name=f"fk_{table}_conversation",
        ondelete="CASCADE",
    )


class _OrgColumns:
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class PortalCopilotConversation(_OrgColumns, UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "portal_copilot_conversations"
    __candidate_pii__ = True  # a Candidate's own conversation (erasure ``portal_copilot.candidate``)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("principal_type", PRINCIPAL_TYPES), name="principal_type"),
        CheckConstraint(_ONE_ORG, name="one_org"),
        CheckConstraint(_in("status", CONVERSATION_STATUSES), name="status"),
        CheckConstraint(_in("agent_key", PORTAL_AGENT_KEYS), name="agent_key"),
        Index(
            "ix_portal_copilot_conversations_principal",
            "tenant_id",
            "principal_type",
            "principal_id",
            text("last_message_at DESC"),
        ),
        Index("ix_portal_copilot_conversations_expires_at", "expires_at"),
    )

    principal_type: Mapped[str] = mapped_column(String(12), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(30), nullable=False)
    title_sealed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortalCopilotMessage(_OrgColumns, UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "portal_copilot_messages"
    __candidate_pii__ = True
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "conversation_id", "seq", name="uq_portal_copilot_messages_seq"),
        _conv_fk("portal_copilot_messages"),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="role"),
        CheckConstraint(
            "((client_id IS NOT NULL)::int + (vendor_id IS NOT NULL)::int + (candidate_id IS NOT NULL)::int) = 1",
            name="one_org",
        ),
        Index("ix_portal_copilot_messages_run_id", "run_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content_sealed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortalCopilotFeedback(_OrgColumns, UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "portal_copilot_feedback"
    __candidate_pii__ = True
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "principal_id", name="uq_portal_copilot_feedback_run"),
        _conv_fk("portal_copilot_feedback"),
        CheckConstraint(_in("principal_type", PRINCIPAL_TYPES), name="principal_type"),
        CheckConstraint(_ONE_ORG, name="one_org"),
        CheckConstraint(_in("rating", RATINGS), name="rating"),
        CheckConstraint(f"reason IS NULL OR {_in('reason', FEEDBACK_REASONS)}", name="reason"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(12), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rating: Mapped[str] = mapped_column(String(4), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(14), nullable=True)
    comment_sealed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class PortalCopilotSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "portal_copilot_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_portal_copilot_settings_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_portal_copilot_settings_updated_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("questions_per_hour_external BETWEEN 5 AND 120", name="qph_external"),
        CheckConstraint("questions_per_hour_candidate BETWEEN 5 AND 60", name="qph_candidate"),
        CheckConstraint("retention_days BETWEEN 7 AND 90", name="retention"),
    )

    client_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    vendor_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    candidate_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    questions_per_hour_external: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("30")
    )
    questions_per_hour_candidate: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("15")
    )
    retention_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("30"))
    handoff_queue_role_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    disclosure_extra: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = [
    "CONVERSATION_STATUSES",
    "DEFAULT_QPH_CANDIDATE",
    "DEFAULT_QPH_EXTERNAL",
    "DEFAULT_RETENTION_DAYS",
    "FEEDBACK_REASONS",
    "MESSAGE_ROLES",
    "PORTAL_AGENT_KEYS",
    "PRINCIPAL_TYPES",
    "RATINGS",
    "PortalCopilotConversation",
    "PortalCopilotFeedback",
    "PortalCopilotMessage",
    "PortalCopilotSettings",
]
