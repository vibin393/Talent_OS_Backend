# ruff: noqa: E501
"""Epic 21 (Compliance) 208_02: credentials (versioned, encrypted numbers + last4 / blind index, AD-35;
Aadhaar = last4 only, NFR-24), credential alerts (dedupe per offset) and recompute runs (one per local date).

``credentials`` / ``credential_alerts``: worker org RLS on ``candidate_id`` (client / vendor sessions see none).

Revision ID: 20261005_208_02_credentials
Revises: 20261005_208_01_compliance_cfg
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_208_02_credentials"
down_revision: str | tuple[str, ...] | None = "20261005_208_01_compliance_cfg"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("credentials", "credential_alerts", "compliance_recompute_runs")


def _fk(table: str, col: str, target: str, ondelete: str = "RESTRICT", name: str | None = None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name or f"fk_{table}_{col}_{target}", ondelete=ondelete
    )


def _tenant(table: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("credential_type_id", sa.Uuid(), nullable=False),
        sa.Column("type_key", sa.String(60), nullable=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("number_last4", sa.String(4), nullable=True),
        sa.Column("number_bidx", sa.String(32), nullable=True),
        sa.Column("issuer", sa.String(200), nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("redaction_status", sa.String(20), nullable=True),
        sa.Column("verification_status", sa.String(25), server_default=sa.text("'pending_verification'"), nullable=False),
        sa.Column("expiry_status", sa.String(20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("version_no", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("previous_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), server_default=sa.text("'user'"), nullable=False),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_tenant("credentials"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_credentials_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_credentials_human_id"),
        sa.CheckConstraint(
            "verification_status IN ('pending_verification', 'valid', 'rejected')", name="ck_credentials_verification_status"
        ),
        sa.CheckConstraint("expiry_status IN ('none', 'expiring_soon', 'expired')", name="ck_credentials_expiry_status"),
        sa.CheckConstraint(
            "redaction_status IS NULL OR redaction_status IN ('pending_review', 'confirmed', 'rejected')",
            name="ck_credentials_redaction",
        ),
        sa.CheckConstraint(
            "source IN ('user', 'worker_portal', 'onboarding', 'bgv', 'import')", name="ck_credentials_source"
        ),
        sa.CheckConstraint("number_last4 IS NULL OR length(number_last4) <= 4", name="ck_credentials_last4_len"),
        _fk("credentials", "candidate_id", "candidates", name="fk_credentials_candidate_id_candidates"),
        _fk("credentials", "credential_type_id", "credential_types", name="fk_credentials_credential_type_id"),
        _fk("credentials", "previous_id", "credentials", name="fk_credentials_previous_id"),
    )
    op.create_index("ix_credentials_tenant_id", "credentials", ["tenant_id"])
    op.create_index("ix_credentials_candidate_id", "credentials", ["candidate_id"])
    op.create_index("ix_credentials_type_key", "credentials", ["type_key"])
    op.create_index("ix_credentials_expires_on", "credentials", ["expires_on"])

    op.create_table(
        "credential_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("offset_days", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_tenant("credential_alerts"),
        sa.UniqueConstraint("tenant_id", "credential_id", "offset_days", name="uq_credential_alerts_key"),
        _fk("credential_alerts", "credential_id", "credentials", "CASCADE", name="fk_credential_alerts_credential_id"),
        _fk("credential_alerts", "candidate_id", "candidates", name="fk_credential_alerts_candidate_id_candidates"),
    )
    op.create_index("ix_credential_alerts_tenant_id", "credential_alerts", ["tenant_id"])

    op.create_table(
        "compliance_recompute_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        *_tenant("compliance_recompute_runs"),
        sa.UniqueConstraint("tenant_id", "run_date", name="uq_compliance_recompute_runs_run_date"),
    )

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("credentials", worker_col="candidate_id")
    enable_org_rls("credential_alerts", worker_col="candidate_id")
    enable_org_rls("compliance_recompute_runs")


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
