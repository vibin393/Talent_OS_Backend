# ruff: noqa: E501
"""Epic 46 story 46.1 (FR-232, AD-52): platform ``visa_type_templates`` (+ seed: US H-1B, UK Skilled Worker, UAE
employment visa), tenant ``visa_types`` and append-only ``visa_type_versions``.

``visa_type_templates`` is a platform table (no RLS, SELECT-only for the runtime role, ``PLATFORM_TABLES``). The
tenant tables carry tenant RLS only (no org policy): they are configuration without personal data, and the Worker
portal reads visa type names and stage labels of the Worker's own Cases.

Revision ID: 20261007_310_01_visa_types
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_immutability, disable_rls, enable_immutability, enable_rls
from app.core.settings import get_settings

revision: str = "20261007_310_01_visa_types"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_immigration",)
depends_on: str | Sequence[str] | None = None

VISA_CATEGORIES = "category IN ('work', 'dependent', 'business_travel')"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def upgrade() -> None:
    t = "visa_type_templates"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("country", sa.CHAR(2), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("template_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_reviewed", sa.Date(), nullable=False),
        sa.Column("notice", sa.Text(), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("key", name="uq_visa_type_templates_key"),
        sa.CheckConstraint(VISA_CATEGORIES, name=op.f("ck_visa_type_templates_category")),
    )
    role = get_settings().db_app_user
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {t} FROM {role}")
    op.execute(f"GRANT SELECT ON {t} TO {role}")

    t = "visa_types"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("country", sa.CHAR(2), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("template_key", sa.String(50), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("last_reviewed", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "key", name="uq_visa_types_key"),
        sa.CheckConstraint(VISA_CATEGORIES, name=op.f("ck_visa_types_category")),
    )
    op.create_index("ix_visa_types_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "visa_type_versions"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("visa_type_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("visa_type_id", "version_no", name="uq_visa_type_versions_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "visa_type_id"],
            ["visa_types.tenant_id", "visa_types.id"],
            name="fk_visa_type_versions_visa_type_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_visa_type_versions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_visa_type_versions_visa_type_id", t, ["visa_type_id"])
    enable_rls(t)
    enable_immutability(t, status_column=None)

    from app.services.immigration.templates import sync_seed

    sync_seed(op.get_bind())


def downgrade() -> None:
    disable_immutability("visa_type_versions")
    for t in ("visa_type_versions", "visa_types"):
        disable_rls(t)
        op.drop_table(t)
    op.drop_table("visa_type_templates")
