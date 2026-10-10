# ruff: noqa: E501
"""Epic 21 (Compliance) 208_01: credential types (+ seed for existing tenants), compliance requirements and
compliance settings.

Branch ``p2_compliance`` forks from the foundation contract layer. Configuration tables are internal only,
except ``credential_types`` / ``compliance_settings``, which a Worker session may read (no personal data).

Revision ID: 20261005_208_01_compliance_cfg
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    ORG_POLICY_NAME,
    disable_org_rls,
    disable_rls,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261005_208_01_compliance_cfg"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_compliance",)
depends_on: str | Sequence[str] | None = None

TABLES = ("credential_types", "compliance_requirements", "compliance_settings")

# key, label, category, has_expiry, number_kind, requires_document
SEED_TYPES: tuple[tuple[str, str, str, bool, str, bool], ...] = (
    ("passport", "Passport", "passport", True, "encrypted", True),
    ("visa", "Visa / work permit", "visa_work_permit", True, "encrypted", True),
    ("pan_card", "PAN card", "identity", False, "encrypted", True),
    ("aadhaar", "Aadhaar", "identity", False, "aadhaar_last4", False),
    ("certification", "Certification", "certification", True, "none", True),
    ("professional_licence", "Professional licence", "professional_licence", True, "encrypted", True),
    ("insurance", "Insurance", "insurance", True, "none", True),
    ("client_badge", "Client badge", "client_document", True, "none", False),
)


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [sa.Column("id", sa.Uuid(), nullable=False), sa.Column("tenant_id", sa.Uuid(), nullable=False)]


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
    ]


def seed_types_sql() -> str:
    values = ", ".join(
        f"('{k}', '{label}', '{cat}', {str(exp).lower()}, '{nk}', {str(doc).lower()})"
        for k, label, cat, exp, nk, doc in SEED_TYPES
    )
    return (
        "INSERT INTO credential_types (id, tenant_id, key, label, category, has_expiry, number_kind, requires_document) "
        f"SELECT gen_random_uuid(), t.id, v.key, v.label, v.category, v.has_expiry, v.number_kind, v.requires_document "
        f"FROM tenants t CROSS JOIN (VALUES {values}) AS v(key, label, category, has_expiry, number_kind, requires_document) "
        "ON CONFLICT (tenant_id, key) DO NOTHING"
    )


def upgrade() -> None:
    op.create_table(
        "credential_types",
        *_base(),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("has_expiry", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("number_kind", sa.String(20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("requires_document", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("alert_offsets", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        *_tenant("credential_types"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_credential_types_key"),
        sa.CheckConstraint(
            "category IN ('passport', 'visa_work_permit', 'certification', 'professional_licence', 'insurance', "
            "'client_document', 'identity', 'other')",
            name="ck_credential_types_category",
        ),
        sa.CheckConstraint("number_kind IN ('none', 'encrypted', 'aadhaar_last4')", name="ck_credential_types_number_kind"),
    )
    op.create_index("ix_credential_types_tenant_id", "credential_types", ["tenant_id"])

    op.create_table(
        "compliance_requirements",
        *_base(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("match", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("items", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        *_tenant("compliance_requirements"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_compliance_requirements_client_id_clients",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_compliance_requirements_tenant_id", "compliance_requirements", ["tenant_id"])
    op.create_index("ix_compliance_requirements_client_id", "compliance_requirements", ["client_id"])

    op.create_table(
        "compliance_settings",
        *_base(),
        sa.Column("evaluate_at", sa.String(30), server_default=sa.text("'placement_created'"), nullable=False),
        sa.Column(
            "default_alert_offsets",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{60,30,7}'::integer[]"),
            nullable=False,
        ),
        sa.Column("recompute_hour_local", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        *_ts(),
        *_tenant("compliance_settings"),
        sa.UniqueConstraint("tenant_id", name="uq_compliance_settings_tenant_id"),
        sa.CheckConstraint(
            "evaluate_at IN ('placement_created', 'application_selected')", name="ck_compliance_settings_evaluate_at"
        ),
        sa.CheckConstraint(
            "recompute_hour_local BETWEEN 0 AND 23", name="ck_compliance_settings_recompute_hour_local"
        ),
    )

    op.execute(seed_types_sql())  # existing tenants, before RLS (new tenants: provisioning hook)
    for table in TABLES:
        enable_rls(table)
    enable_org_rls("compliance_requirements")
    # credential_types / compliance_settings: readable by internal and worker sessions (the "My Work" form and
    # the tenant clock), written by internal sessions only.
    expr = "(current_setting('app.org_kind', true) IN ('internal', 'worker'))"
    for table in ("credential_types", "compliance_settings"):
        op.execute(f"DROP POLICY IF EXISTS {ORG_POLICY_NAME} ON {table}")
        op.execute(
            f"CREATE POLICY {ORG_POLICY_NAME} ON {table} AS RESTRICTIVE FOR ALL USING {expr} "
            "WITH CHECK (current_setting('app.org_kind', true) = 'internal')"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
