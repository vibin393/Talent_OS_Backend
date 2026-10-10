# ruff: noqa: E501
"""Epic 20 (BGV & reference checks) 207_01: settings, packages, providers, reference questionnaires; seeds the
``bgv`` and ``reference_check`` consent notices for existing tenants (idempotent). The consent purpose / source
CHECK widening is owned by the foundation (R12, 200_01) and is not repeated here.

Revision ID: 20261005_207_01_bgv_config
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_207_01_bgv_config"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_bgv",)
depends_on: str | Sequence[str] | None = None

TABLES = ("bgv_settings", "bgv_packages", "bgv_providers", "reference_questionnaires")
NOTICES = {
    "bgv": (
        "Background verification",
        "With your consent we verify the information and documents you provide (identity, employment, education, "
        "address and other checks listed for your case), contacting the named sources and verification providers. "
        "Only the data a check needs is shared. You can withdraw consent at any time from the portal; verification "
        "then stops. Evidence is kept for 12 months after the verification decision.",
    ),
    "reference_check": (
        "Reference checks",
        "With your consent we contact the referees you name and ask them a short questionnaire about your work. "
        "Their answers are seen only by the hiring team and are not shared with you or with clients.",
    ),
}


def _std(table: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
    ]


def upgrade() -> None:
    op.create_table(
        "bgv_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("doc_reminder_every_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("discrepancy_response_working_days", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("reference_link_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("reference_reminder_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("reuse_clear_case_days", sa.Integer(), server_default=sa.text("180"), nullable=False),
        sa.Column("provider_link_days", sa.Integer(), server_default=sa.text("21"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_bgv_settings"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_bgv_settings_tenant_id_tenants", ondelete="RESTRICT"),
    )
    op.create_table(
        "bgv_packages",
        *_std("bgv_packages"),  # type: ignore[arg-type]
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("turnaround_target_days", sa.Integer(), nullable=True),
        sa.Column("lawful_confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("lawful_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bgv_packages_tenant_id", "bgv_packages", ["tenant_id"])
    op.create_table(
        "bgv_providers",
        *_std("bgv_providers"),  # type: ignore[arg-type]
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(10), server_default=sa.text("'external'"), nullable=False),
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("contact_email", postgresql.CITEXT(), nullable=True),
        sa.Column("supported_types", postgresql.ARRAY(sa.String(30)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("connector_config_id", sa.Uuid(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("kind IN ('internal', 'external')", name="ck_bgv_providers_kind"),
    )
    op.create_index("ix_bgv_providers_tenant_id", "bgv_providers", ["tenant_id"])
    op.create_table(
        "reference_questionnaires",
        *_std("reference_questionnaires"),  # type: ignore[arg-type]
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("questions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_reference_questionnaires_tenant_id", "reference_questionnaires", ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)  # internal only

    # Idempotent seed of the ``bgv`` / ``reference_check`` notices for every existing tenant (owner bypasses RLS).
    for purpose, (title, body) in NOTICES.items():
        op.execute(
            sa.text(
                """
                INSERT INTO consent_notices (id, tenant_id, purpose, locale, version, title, body_md, active, created_at, updated_at)
                SELECT gen_random_uuid(), t.id, :purpose, 'en', 1, :title, :body, true, now(), now()
                FROM tenants t
                WHERE NOT EXISTS (
                    SELECT 1 FROM consent_notices n WHERE n.tenant_id = t.id AND n.purpose = :purpose AND n.locale = 'en'
                )
                """
            ).bindparams(purpose=purpose, title=title, body=body)
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
