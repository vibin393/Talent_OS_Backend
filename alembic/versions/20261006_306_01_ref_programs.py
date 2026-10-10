"""Epic 42 story 42.1 (FR-213, AD-50): ``referral_settings`` (one row per tenant) and ``referral_programs``.

Configuration without personal data: tenant RLS only (Worker referral flows read programs through an internal
session after the principal check). Money is ``NUMERIC(14,2)`` (AD-20).

Revision ID: 20261006_306_01_ref_programs
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_306_01_ref_programs"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_referrals",)
depends_on: str | Sequence[str] | None = None

ALL_DEMANDS = """'{"all": true}'::jsonb"""


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    t = "referral_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("duplicate_activity_months", sa.Integer(), server_default=sa.text("6"), nullable=False),
        sa.Column("referee_link_days", sa.Integer(), server_default=sa.text("14"), nullable=False),
        sa.Column("share_link_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("default_owner_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payout_export_currency_default", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False
        ),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_referral_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_referral_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "default_owner_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_referral_settings_default_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "duplicate_activity_months BETWEEN 1 AND 60", name=op.f("ck_referral_settings_dup_months")
        ),
        sa.CheckConstraint(
            "referee_link_days BETWEEN 1 AND 90", name=op.f("ck_referral_settings_referee_days")
        ),
        sa.CheckConstraint("share_link_days BETWEEN 1 AND 365", name=op.f("ck_referral_settings_share_days")),
    )
    op.create_index("ix_referral_settings_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "referral_programs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("eligible_referrers", sa.String(10), server_default=sa.text("'both'"), nullable=False),
        sa.Column(
            "eligible_demands", postgresql.JSONB(), server_default=sa.text(ALL_DEMANDS), nullable=False
        ),
        sa.Column("reward_rules", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("default_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("joining_condition_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("exclusions", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_referral_programs"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_referral_programs_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_referral_programs_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "eligible_referrers IN ('users', 'workers', 'both')",
            name=op.f("ck_referral_programs_eligible_referrers"),
        ),
        sa.CheckConstraint(
            "default_amount IS NULL OR default_amount > 0", name=op.f("ck_referral_programs_default_amount")
        ),
        sa.CheckConstraint(
            "joining_condition_days BETWEEN 0 AND 730", name=op.f("ck_referral_programs_condition_days")
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name=op.f("ck_referral_programs_validity")
        ),
    )
    op.create_index("ix_referral_programs_tenant_id", t, ["tenant_id"])
    op.create_index("ix_referral_programs_active", t, ["active", "priority"])
    enable_rls(t)


def downgrade() -> None:
    for t in ("referral_programs", "referral_settings"):
        disable_rls(t)
        op.drop_table(t)
