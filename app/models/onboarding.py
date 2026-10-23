# ruff: file-ignore[line-too-long]
"""Digital onboarding (Epic 23, AD-27 / AD-35). Migrations ``20261005_210_01..02`` (branch ``p2_onboarding``).

* ``onboarding_settings`` / ``onboarding_templates`` / ``onboarding_template_tasks`` -- tenant configuration
  (internal-only org RLS).
* ``onboarding_cases`` -- one per Placement (``placement_id`` UNIQUE); a holding row (``initialized_at`` NULL)
  keeps compliance requirements that arrive before the ``PlacementCreated`` handler ran.
* ``onboarding_tasks`` -- the checklist of a case (worker org RLS on the inherited ``candidate_id``; a restrictive
  policy hides ``assignee='user'`` rows from Worker sessions).
* ``onboarding_acknowledgements`` -- append-only policy acknowledgements (immutability trigger).

Never stored here: bank account numbers, PAN, Aadhaar / passport numbers (placements / compliance own them).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
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
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TASK_KINDS = (
    "personal_details",
    "bank_details",
    "tax_details",
    "emergency_contact",
    "document_upload",
    "esign",
    "policy_ack",
    "client_form",
    "credential",
    "internal",
)
WORKER_KINDS = tuple(k for k in TASK_KINDS if k != "internal")
ASSIGNEES = ("worker", "user")
OWNER_KINDS = ("role", "placement_account_manager", "placement_recruiter", "case_owner")
CASE_STATUSES = ("not_started", "in_progress", "complete", "cancelled")
TASK_STATUSES = ("pending", "submitted", "returned", "approved", "waived")
TASK_SOURCES = ("template", "compliance", "user")
DECIDER_KINDS = ("user", "system")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class OnboardingSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_onboarding_settings_tenant_id"),)

    auto_invite: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    worker_reminder_every_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    default_owner_by_role: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    default_hr_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    default_tenant_signatory_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    reuse_validity_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("365"))


class OnboardingTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_onboarding_templates_tenant_id_id"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    worker_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class OnboardingTemplateTask(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_template_tasks"
    __table_args__ = (
        UniqueConstraint("template_id", "key", name="uq_onboarding_template_tasks_template_id_key"),
        CheckConstraint(_in("kind", TASK_KINDS), name="ck_onboarding_template_tasks_kind"),
        CheckConstraint(_in("assignee", ASSIGNEES), name="ck_onboarding_template_tasks_assignee"),
        CheckConstraint(
            f"owner_kind IS NULL OR {_in('owner_kind', OWNER_KINDS)}",
            name="ck_onboarding_template_tasks_owner_kind",
        ),
        _tfk("template_id", "onboarding_templates", "fk_onboarding_template_tasks_template", "CASCADE"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    assignee: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'worker'"))
    owner_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    owner_role_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    due_offset_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    auto_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reusable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class OnboardingCase(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_onboarding_cases_tenant_id_id"),
        UniqueConstraint("tenant_id", "placement_id", name="uq_onboarding_cases_placement_id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_onboarding_cases_human_id"),
        CheckConstraint(_in("status", CASE_STATUSES), name="ck_onboarding_cases_status"),
        CheckConstraint("progress_pct BETWEEN 0 AND 100", name="ck_onboarding_cases_progress_pct"),
        _tfk("placement_id", "placements", "fk_onboarding_cases_placement"),
        _tfk("candidate_id", "candidates", "fk_onboarding_cases_candidate"),
        _tfk("template_id", "onboarding_templates", "fk_onboarding_cases_template", "RESTRICT"),
        Index("ix_onboarding_cases_candidate_id", "candidate_id"),
    )

    human_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'not_started'"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pending_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingTask(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_tasks"
    __pii_fields__ = ("draft", "data", "return_comment")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_onboarding_tasks_tenant_id_id"),
        UniqueConstraint("case_id", "key", name="uq_onboarding_tasks_case_id_key"),
        CheckConstraint(_in("kind", TASK_KINDS), name="ck_onboarding_tasks_kind"),
        CheckConstraint(_in("assignee", ASSIGNEES), name="ck_onboarding_tasks_assignee"),
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_onboarding_tasks_status"),
        CheckConstraint(_in("source", TASK_SOURCES), name="ck_onboarding_tasks_source"),
        CheckConstraint(
            f"decided_by_kind IS NULL OR {_in('decided_by_kind', DECIDER_KINDS)}",
            name="ck_onboarding_tasks_decided_by_kind",
        ),
        _tfk("case_id", "onboarding_cases", "fk_onboarding_tasks_case", "CASCADE"),
        Index("ix_onboarding_tasks_candidate_id", "candidate_id"),
        Index("ix_onboarding_tasks_p1_task_id", "p1_task_id"),
        Index("ix_onboarding_tasks_signature_request_id", "signature_request_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    assignee: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'worker'"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    p1_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'template'"))
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reusable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    signature_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    return_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    waived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reused_from_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingAcknowledgement(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_acknowledgements"
    __pii_fields__ = ("ip", "user_agent")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __table_args__ = (
        _tfk("task_id", "onboarding_tasks", "fk_onboarding_acknowledgements_task"),
        Index("ix_onboarding_acknowledgements_candidate_id", "candidate_id"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rendered_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
