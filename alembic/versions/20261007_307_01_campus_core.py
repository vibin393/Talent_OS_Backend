# ruff: noqa: E501
"""Epic 43 story 43.1 (FR-217): ``campus_settings``, ``institutions``, ``institution_contacts`` (placement officers,
coordinator decision 13), ``campus_drives``, ``drive_demands``, ``drive_institutions``.

Internal only: tenant RLS plus ``enable_org_rls`` without org columns (portal sessions see nothing). FKs go only to
the foundation hub ``assessments`` and P1 tables. CHECK names are wrapped in ``op.f`` (foundation note).

Revision ID: 20261007_307_01_campus_core
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_307_01_campus_core"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_campus",)
depends_on: str | Sequence[str] | None = None

E164 = "^\\+[1-9][0-9]{6,14}$"


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str, *, pk: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]
    if pk:
        cols = [
            sa.Column("id", sa.Uuid(), nullable=False),
            *cols,
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        ]
    return cols


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def _internal(table: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    enable_rls(table)
    enable_org_rls(table)


def upgrade() -> None:
    t = "campus_settings"
    op.create_table(
        t,
        *_tenant(t, pk=False),
        sa.Column("register_ip_limit", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("register_drive_limit", sa.Integer(), server_default=sa.text("3000"), nullable=False),
        sa.Column("avg_interview_minutes", sa.Integer(), server_default=sa.text("20"), nullable=False),
        sa.Column("poster_footer", sa.String(300), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_campus_settings"),
        sa.CheckConstraint("register_ip_limit BETWEEN 1 AND 100", name=op.f("ck_campus_settings_ip_limit")),
        sa.CheckConstraint("register_drive_limit BETWEEN 100 AND 20000", name=op.f("ck_campus_settings_drive_limit")),
        sa.CheckConstraint("avg_interview_minutes BETWEEN 5 AND 180", name=op.f("ck_campus_settings_avg_minutes")),
    )
    _internal(t)

    t = "institutions"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("state", sa.String(120), nullable=True),
        sa.Column("country", sa.String(2), server_default=sa.text("'IN'"), nullable=False),
        sa.Column("tier", sa.String(10), server_default=sa.text("'other'"), nullable=False),
        sa.Column("programs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_institutions_tenant_id_name"),
        sa.CheckConstraint(_in("tier", ("tier_1", "tier_2", "tier_3", "other")), name=op.f("ck_institutions_tier")),
        sa.CheckConstraint(_in("status", ("active", "archived")), name=op.f("ck_institutions_status")),
    )
    _internal(t)

    t = "institution_contacts"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("institution_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint(f"phone IS NULL OR phone ~ '{E164}'", name=op.f("ck_institution_contacts_phone_e164")),
        _fk("fk_institution_contacts_institution", "institution_id", "institutions"),
    )
    op.create_index("ix_institution_contacts_institution_id", t, ["institution_id"])
    op.create_index(
        "uq_institution_contacts_email", t, ["institution_id", "email"], unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )  # fmt: skip
    _internal(t)

    t = "campus_drives"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("batch_year", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("registration_opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assessment_window", postgresql.JSONB(), nullable=True),
        sa.Column("interview_window", postgresql.JSONB(), nullable=True),
        sa.Column("form_schema", postgresql.JSONB(), server_default=sa.text("""'{"fields": []}'::jsonb"""), nullable=False),
        sa.Column("eligibility_definition", postgresql.JSONB(), nullable=True),
        sa.Column("eligibility_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("interview_stage_template_id", sa.Uuid(), nullable=True),
        sa.Column("offer_template_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            _in("status", ("draft", "registration_open", "in_progress", "closed", "cancelled")),
            name=op.f("ck_campus_drives_status"),
        ),
        sa.CheckConstraint("batch_year BETWEEN 2000 AND 2100", name=op.f("ck_campus_drives_batch_year")),
        sa.CheckConstraint(
            "registration_closes_at IS NULL OR registration_opens_at IS NULL OR registration_closes_at > registration_opens_at",
            name=op.f("ck_campus_drives_registration_window"),
        ),
        _fk("fk_campus_drives_owner_id_users", "owner_id", "users"),
        _fk("fk_campus_drives_assessment_id", "assessment_id", "assessments"),
        _fk("fk_campus_drives_stage_template", "interview_stage_template_id", "interview_stage_templates"),
        _fk("fk_campus_drives_offer_template", "offer_template_id", "offer_templates"),
    )
    op.create_index("ix_campus_drives_status", t, ["status"])
    op.create_index("ix_campus_drives_owner_id", t, ["owner_id"])
    _internal(t)

    t = "drive_demands"
    op.create_table(
        t,
        *_tenant(t, pk=False),
        sa.Column("drive_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("drive_id", "demand_id", name="pk_drive_demands"),
        _fk("fk_drive_demands_drive_id", "drive_id", "campus_drives", "CASCADE"),
        _fk("fk_drive_demands_demand_id", "demand_id", "demands"),
    )
    op.create_index("ix_drive_demands_demand_id", t, ["demand_id"])
    _internal(t)

    t = "drive_institutions"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("drive_id", sa.Uuid(), nullable=False),
        sa.Column("institution_id", sa.Uuid(), nullable=False),
        sa.Column("registration_link_id", sa.Uuid(), nullable=True),
        sa.Column("token_sealed", sa.Text(), nullable=True),
        sa.Column("qr_file_id", sa.Uuid(), nullable=True),
        sa.Column("registration_cap", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("drive_id", "institution_id", name="uq_drive_institutions_pair"),
        sa.CheckConstraint(_in("status", ("active", "paused")), name=op.f("ck_drive_institutions_status")),
        sa.CheckConstraint("registration_cap IS NULL OR registration_cap >= 1", name=op.f("ck_drive_institutions_cap")),
        _fk("fk_drive_institutions_drive_id", "drive_id", "campus_drives", "CASCADE"),
        _fk("fk_drive_institutions_institution_id", "institution_id", "institutions"),
        _fk("fk_drive_institutions_qr_file_id", "qr_file_id", "stored_files"),
        sa.ForeignKeyConstraint(
            ["registration_link_id"], ["secure_links.id"], name="fk_drive_institutions_link_id", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_drive_institutions_institution_id", t, ["institution_id"])
    _internal(t)


def downgrade() -> None:
    for t in (
        "drive_institutions",
        "drive_demands",
        "campus_drives",
        "institution_contacts",
        "institutions",
        "campus_settings",
    ):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
