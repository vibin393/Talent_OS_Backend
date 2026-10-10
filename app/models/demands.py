"""Demand tables (Epic 4, stories 4.1-4.6). Tenant-owned with forced RLS; tenant-internal references use
composite foreign keys ``(tenant_id, x_id) -> x(tenant_id, id)`` because FK checks bypass RLS.

Migrations: 20261001_41 (settings, demands, skills), 20261001_43 (JD versions, postings, bias terms,
documents), 20261001_44 (assignments, SLA targets and clocks).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

DEMAND_STATUSES = ("draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed")
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")
WORK_MODES = ("onsite", "hybrid", "remote")
PRIORITIES = ("low", "medium", "high", "critical")
RATE_UNITS = ("hour", "day", "month", "year")
DEMAND_VISIBILITIES = ("external", "internal", "both")  # Epic 36 (300_03): careers visibility (AD-50)
SKILL_KINDS = ("mandatory", "preferred")
SKILL_REF_KINDS = ("global", "tenant")
JD_KINDS = (
    "full",
    "short",
    "posting",
    "recruiter_summary",
    "screening_questions",
    "interview_questions",
    "boolean",
)
AUTHOR_TYPES = ("user", "ai")
DOCUMENT_KINDS = ("intake_source", "attachment")
SLA_MILESTONES = ("assignment", "first_submission", "first_interview", "client_feedback")
SLA_CLOCK_TYPES = ("business", "calendar")
SLA_STATUSES = ("healthy", "at_risk", "breached", "met")

DEFAULT_WORK_WEEK: dict[str, Any] = {
    day: {"start": "09:00", "end": "18:00"} for day in ("mon", "tue", "wed", "thu", "fri")
}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


MONEY = Numeric(14, 2)


# ================================================================ settings
class DemandSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant: REQ prefix, work week and holidays (business-hours calendar)."""

    __tablename__ = "demand_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (CheckConstraint("req_prefix ~ '^[A-Z][A-Z0-9]{0,9}$'", name="req_prefix"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    req_prefix: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'REQ'"))
    work_week: Mapped[dict[str, Any]] = mapped_column(
        JSONB(),
        nullable=False,
        server_default=text(
            """'{"mon": {"start": "09:00", "end": "18:00"}, "tue": {"start": "09:00", "end": "18:00"}, """
            """"wed": {"start": "09:00", "end": "18:00"}, "thu": {"start": "09:00", "end": "18:00"}, """
            """"fri": {"start": "09:00", "end": "18:00"}}'::jsonb"""
        ),
    )
    holidays: Mapped[list[date]] = mapped_column(
        ARRAY(Date()), nullable=False, server_default=text("'{}'::date[]")
    )


# ================================================================ demands
class Demand(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demands"
    __table_args__ = (
        UniqueConstraint("tenant_id", "req_id"),
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("client_id", "clients", "fk_demands_client_id_clients"),
        _tenant_fk("hiring_manager_contact_id", "contacts", "fk_demands_hiring_manager_contact_id_contacts"),
        _tenant_fk("owner_id", "users", "fk_demands_owner_id_users"),
        CheckConstraint(_in("status", DEMAND_STATUSES), name="status"),
        CheckConstraint(f"employment_type IS NULL OR {_in('employment_type', EMPLOYMENT_TYPES)}", name="emp"),
        CheckConstraint(f"work_mode IS NULL OR {_in('work_mode', WORK_MODES)}", name="work_mode"),
        CheckConstraint(_in("priority", PRIORITIES), name="priority"),
        CheckConstraint(f"rate_unit IS NULL OR {_in('rate_unit', RATE_UNITS)}", name="rate_unit"),
        CheckConstraint("positions >= 1", name="positions"),
        CheckConstraint("travel_percent IS NULL OR travel_percent BETWEEN 0 AND 100", name="travel"),
        CheckConstraint(
            "min_experience_years IS NULL OR max_experience_years IS NULL "
            "OR min_experience_years <= max_experience_years",
            name="experience_range",
        ),
        CheckConstraint(
            "pay_rate_min IS NULL OR pay_rate_max IS NULL OR pay_rate_min <= pay_rate_max", name="pay_range"
        ),
        Index("ix_demands_tenant_id_status", "tenant_id", "status"),
        Index("ix_demands_client_id", "client_id"),
        Index("ix_demands_owner_id", "owner_id"),
        # Epic 36 (300_03): careers visibility and Phase 3 hub links (set only via demands_service.create_draft).
        CheckConstraint(_in("visibility", DEMAND_VISIBILITIES), name="visibility"),
        _tenant_fk(
            "workforce_plan_line_id",
            "workforce_plan_lines",
            "fk_demands_workforce_plan_line_id_workforce_plan_lines",
        ),
        _tenant_fk("project_role_id", "project_roles", "fk_demands_project_role_id_project_roles"),
        Index("ix_demands_workforce_plan_line_id", "workforce_plan_line_id"),
        Index("ix_demands_project_role_id", "project_role_id"),
    )

    req_id: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    end_client: Mapped[str | None] = mapped_column(String(300), nullable=True)
    hiring_manager_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    positions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    min_experience_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    max_experience_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    education: Mapped[str | None] = mapped_column(String(500), nullable=True)
    certifications: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    work_authorization: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    clearance: Mapped[str | None] = mapped_column(String(200), nullable=True)
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    shift: Mapped[str | None] = mapped_column(String(100), nullable=True)
    travel_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    target_closure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'medium'"))
    bill_rate: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    pay_rate_min: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    pay_rate_max: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    submission_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ordered [{name, kind}]
    interview_plan: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    status_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ai_extracted_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    intake_ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_jd_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Progress counters, written only by the pipeline hooks (actor system).
    applications_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submissions_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    interviews_scheduled_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    offers_released_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    positions_filled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- Epic 36 (300_03)
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'external'"))
    workforce_plan_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    project_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DemandSkill(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """``skill_id`` is a plain uuid (no FK in Epic 4): a global ``skills`` or tenant ``tenant_skills`` id,
    told apart by ``skill_ref_kind``. ``skill_label`` always holds the text as entered/resolved."""

    __tablename__ = "demand_skills"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_demand_skills_demand_id_demands", "CASCADE"),
        CheckConstraint(_in("kind", SKILL_KINDS), name="kind"),
        CheckConstraint(f"skill_ref_kind IS NULL OR {_in('skill_ref_kind', SKILL_REF_KINDS)}", name="ref"),
        Index("ix_demand_skills_demand_id", "demand_id"),
        Index("ix_demand_skills_skill_id", "skill_id"),
        # Epic 37 (301_02, FR-190): minimum proficiency and per-skill graph expansion.
        CheckConstraint("min_proficiency IS NULL OR min_proficiency BETWEEN 1 AND 5", name="min_proficiency"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    skill_ref_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    skill_label: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    min_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # ---- Epic 37 (301_02, FR-190): written through the P1 demand skill endpoints (additive fields).
    min_proficiency: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    expand: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


# ================================================================ JD, postings, bias, documents
class JdVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "jd_versions"
    __table_args__ = (
        UniqueConstraint("demand_id", "version_no"),
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("demand_id", "demands", "fk_jd_versions_demand_id_demands", "CASCADE"),
        CheckConstraint(_in("kind", JD_KINDS), name="kind"),
        CheckConstraint(_in("author_type", AUTHOR_TYPES), name="author_type"),
    )
    __audit_exclude__ = ("content",)

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author_type: Mapped[str] = mapped_column(String(10), nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DemandPosting(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_postings"
    __table_args__ = (
        UniqueConstraint("demand_id"),
        _tenant_fk("demand_id", "demands", "fk_demand_postings_demand_id_demands", "CASCADE"),
        _tenant_fk("jd_version_id", "jd_versions", "fk_demand_postings_jd_version_id_jd_versions"),
        Index("ix_demand_postings_live", "is_live"),
        # Epic 9 career-site indexes (migration 20261001_90).
        Index("ix_demand_postings_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_demand_postings_tenant_live_published", "tenant_id", "is_live", text("published_at DESC")),
    )
    __audit_exclude__ = ("search_tsv",)

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    jd_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    show_rate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    show_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    show_client_name: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unpublished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 9 (migration 20261001_90): career-site URL slug, apply questions (Epic 6 question schema, max 10)
    # and the posting search vector (refreshed by ``portal.careers.refresh_posting_search``).
    slug: Mapped[str | None] = mapped_column(String(80), nullable=True)
    apply_questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, nullable=True, deferred=True)


class BiasedTerm(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Tenant additions (``enabled``) and disables (``enabled=false`` for a default term)."""

    __tablename__ = "biased_terms"
    __table_args__ = (UniqueConstraint("tenant_id", "term"),)

    term: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class DemandDocument(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_documents"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_demand_documents_demand_id_demands", "CASCADE"),
        CheckConstraint(_in("kind", DOCUMENT_KINDS), name="kind"),
        Index("ix_demand_documents_demand_id", "demand_id"),
    )
    __audit_exclude__ = ("file_name", "storage_key")

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Set when the file went through the shared upload pipeline (``stored_files``), else NULL (pasted text).
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ assignment
class DemandAssignment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_assignments"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_demand_assignments_demand_id_demands", "CASCADE"),
        _tenant_fk("user_id", "users", "fk_demand_assignments_user_id_users"),
        _tenant_fk("team_id", "teams", "fk_demand_assignments_team_id_teams"),
        CheckConstraint("(user_id IS NULL) <> (team_id IS NULL)", name="one_assignee"),
        CheckConstraint("NOT is_primary OR user_id IS NOT NULL", name="primary_is_user"),
        Index(
            "uq_demand_assignments_primary",
            "demand_id",
            unique=True,
            postgresql_where=text("is_primary AND removed_at IS NULL"),
        ),
        Index("ix_demand_assignments_demand_id", "demand_id"),
        Index("ix_demand_assignments_user_id", "user_id"),
        Index("ix_demand_assignments_team_id", "team_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_application_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_submission_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ SLA
# ``SlaTarget`` moved to ``app.models.sla`` (Epic 13, R14); re-exported here for Phase 1 imports.
from app.models.sla import SlaTarget as SlaTarget  # noqa: E402


class DemandSlaClock(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """One running/finished SLA clock. The target is snapshotted when the clock starts."""

    __tablename__ = "demand_sla_clocks"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_demand_sla_clocks_demand_id_demands", "CASCADE"),
        CheckConstraint(_in("milestone", SLA_MILESTONES), name="milestone"),
        CheckConstraint(_in("status", SLA_STATUSES), name="status"),
        CheckConstraint(_in("clock", SLA_CLOCK_TYPES), name="clock"),
        Index(
            "uq_demand_sla_clocks_milestone",
            "demand_id",
            "milestone",
            "ref_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_demand_sla_clocks_open", "completed_at"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    milestone: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'healthy'"))
    last_notified_status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'healthy'")
    )
    target_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    clock: Mapped[str] = mapped_column(String(10), nullable=False)
    at_risk_percent: Mapped[int] = mapped_column(Integer, nullable=False)
