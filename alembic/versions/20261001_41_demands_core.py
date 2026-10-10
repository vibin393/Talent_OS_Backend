# ruff: noqa: E501
"""Demand settings, demands and demand skills (story 4.1) (tenant-owned, RLS).

Revision ID: 20261001_41
Revises: 20261001_124
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_41"
down_revision: str | tuple[str, ...] | None = "20261001_124"
branch_labels: str | Sequence[str] | None = ("demands",)
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("demand_settings", "demands", "demand_skills")


def upgrade() -> None:
    op.create_table(
        "demand_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("req_prefix", sa.String(length=10), server_default=sa.text("'REQ'"), nullable=False),
        sa.Column(
            "work_week",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(
                '\'{"mon": {"start": "09:00", "end": "18:00"}, "tue": {"start": "09:00", "end": "18:00"}, "wed": {"start": "09:00", "end": "18:00"}, "thu": {"start": "09:00", "end": "18:00"}, "fri": {"start": "09:00", "end": "18:00"}}\'::jsonb'
            ),
            nullable=False,
        ),
        sa.Column("holidays", sa.ARRAY(sa.Date()), server_default=sa.text("'{}'::date[]"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("req_prefix ~ '^[A-Z][A-Z0-9]{0,9}$'", name=op.f("ck_demand_settings_req_prefix")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_demand_settings")),
    )
    op.create_table(
        "demands",
        sa.Column("req_id", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("end_client", sa.String(length=300), nullable=True),
        sa.Column("hiring_manager_contact_id", sa.Uuid(), nullable=True),
        sa.Column("positions", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("department", sa.String(length=200), nullable=True),
        sa.Column("employment_type", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("work_mode", sa.String(length=10), nullable=True),
        sa.Column("min_experience_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("max_experience_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("education", sa.String(length=500), nullable=True),
        sa.Column(
            "certifications", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column(
            "work_authorization", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("clearance", sa.String(length=200), nullable=True),
        sa.Column("languages", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("shift", sa.String(length=100), nullable=True),
        sa.Column("travel_percent", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("target_closure_date", sa.Date(), nullable=True),
        sa.Column("priority", sa.String(length=10), server_default=sa.text("'medium'"), nullable=False),
        sa.Column("bill_rate", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("pay_rate_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("pay_rate_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("rate_unit", sa.String(length=10), nullable=True),
        sa.Column("submission_limit", sa.Integer(), nullable=True),
        sa.Column(
            "interview_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("status_reason_code", sa.String(length=50), nullable=True),
        sa.Column("status_comment", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("business_unit_id", sa.Uuid(), nullable=True),
        sa.Column(
            "ai_extracted_fields", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("intake_ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("published_jd_version_id", sa.Uuid(), nullable=True),
        sa.Column("applications_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submissions_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("interviews_scheduled_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("offers_released_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("positions_filled", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "employment_type IS NULL OR employment_type IN ('contract', 'contract_to_hire', 'permanent', 'freelance')",
            name=op.f("ck_demands_emp"),
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')", name=op.f("ck_demands_priority")
        ),
        sa.CheckConstraint(
            "rate_unit IS NULL OR rate_unit IN ('hour', 'day', 'month', 'year')",
            name=op.f("ck_demands_rate_unit"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'open', 'on_hold', 'filled', 'cancelled', 'closed')",
            name=op.f("ck_demands_status"),
        ),
        sa.CheckConstraint(
            "work_mode IS NULL OR work_mode IN ('onsite', 'hybrid', 'remote')",
            name=op.f("ck_demands_work_mode"),
        ),
        sa.CheckConstraint(
            "min_experience_years IS NULL OR max_experience_years IS NULL OR min_experience_years <= max_experience_years",
            name=op.f("ck_demands_experience_range"),
        ),
        sa.CheckConstraint(
            "pay_rate_min IS NULL OR pay_rate_max IS NULL OR pay_rate_min <= pay_rate_max",
            name=op.f("ck_demands_pay_range"),
        ),
        sa.CheckConstraint("positions >= 1", name=op.f("ck_demands_positions")),
        sa.CheckConstraint(
            "travel_percent IS NULL OR travel_percent BETWEEN 0 AND 100", name=op.f("ck_demands_travel")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_demands_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "hiring_manager_contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_demands_hiring_manager_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_demands_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_demands_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demands")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_demands_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "req_id", name=op.f("uq_demands_tenant_id_req_id")),
    )
    op.create_index("ix_demands_client_id", "demands", ["client_id"], unique=False)
    op.create_index("ix_demands_owner_id", "demands", ["owner_id"], unique=False)
    op.create_index(op.f("ix_demands_tenant_id"), "demands", ["tenant_id"], unique=False)
    op.create_index("ix_demands_tenant_id_status", "demands", ["tenant_id", "status"], unique=False)
    op.create_table(
        "demand_skills",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("skill_ref_kind", sa.String(length=10), nullable=True),
        sa.Column("skill_label", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("min_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('mandatory', 'preferred')", name=op.f("ck_demand_skills_kind")),
        sa.CheckConstraint(
            "skill_ref_kind IS NULL OR skill_ref_kind IN ('global', 'tenant')",
            name=op.f("ck_demand_skills_ref"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_skills_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_skills_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_skills")),
    )
    op.create_index("ix_demand_skills_demand_id", "demand_skills", ["demand_id"], unique=False)
    op.create_index("ix_demand_skills_skill_id", "demand_skills", ["skill_id"], unique=False)
    op.create_index(op.f("ix_demand_skills_tenant_id"), "demand_skills", ["tenant_id"], unique=False)
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
