# ruff: noqa: E501
"""Epic 37 story 37.1 (FR-187, AD-41): skills graph -- platform ``skill_edges``, tenant ``tenant_skill_edges``,
``skill_graph_settings``, and the ESCO-derived platform seed (``services/skills/data/skill_graph_seed.yaml``).

``skill_edges`` is a platform table (no RLS, SELECT-only for the runtime role, ``core.tenancy.PLATFORM_TABLES``).
The tenant tables are internal only: tenant RLS plus ``enable_org_rls`` without org columns.

The seed adds skills / aliases to the P1 ``skills`` catalog (a superset of the P1 seed); downgrade keeps those
rows (other tables may reference them) and drops only the graph tables.

Revision ID: 20261007_301_01_skill_graph
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261007_301_01_skill_graph"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_skills",)
depends_on: str | Sequence[str] | None = None

KINDS = "kind IN ('parent_of', 'related_to', 'implies')"
WEIGHT = "weight > 0 AND weight <= 1"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    # ---------------------------------------------------------------- skill_edges (platform)
    t = "skill_edges"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_skill_id", sa.Uuid(), nullable=False),
        sa.Column("to_skill_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("weight", sa.Numeric(3, 2), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("seed_version", sa.String(20), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_skill_edges"),
        sa.ForeignKeyConstraint(
            ["from_skill_id"], ["skills.id"], name="fk_skill_edges_from_skill_id_skills", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_skill_id"], ["skills.id"], name="fk_skill_edges_to_skill_id_skills", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "from_skill_id", "to_skill_id", "kind", name="uq_skill_edges_from_skill_id_to_skill_id_kind"
        ),
        sa.CheckConstraint(KINDS, name=op.f("ck_skill_edges_kind")),
        sa.CheckConstraint("source IN ('seed', 'esco', 'curated')", name=op.f("ck_skill_edges_source")),
        sa.CheckConstraint(WEIGHT, name=op.f("ck_skill_edges_weight")),
        sa.CheckConstraint("from_skill_id <> to_skill_id", name=op.f("ck_skill_edges_not_self")),
    )
    op.create_index("ix_skill_edges_to_skill_id", t, ["to_skill_id"])
    role = get_settings().db_app_user
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {t} FROM {role}")
    op.execute(f"GRANT SELECT ON {t} TO {role}")

    # ---------------------------------------------------------------- tenant_skill_edges
    t = "tenant_skill_edges"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("from_skill_id", sa.Uuid(), nullable=True),
        sa.Column("from_tenant_skill_id", sa.Uuid(), nullable=True),
        sa.Column("to_skill_id", sa.Uuid(), nullable=True),
        sa.Column("to_tenant_skill_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("weight", sa.Numeric(3, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_skill_edges"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_skill_edges_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_skill_edges_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["from_skill_id"], ["skills.id"], name="fk_tenant_skill_edges_from_skill_id_skills", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_skill_id"], ["skills.id"], name="fk_tenant_skill_edges_to_skill_id_skills", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "from_tenant_skill_id"],
            ["tenant_skills.tenant_id", "tenant_skills.id"],
            name="fk_tenant_skill_edges_from_tenant_skill_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "to_tenant_skill_id"],
            ["tenant_skills.tenant_id", "tenant_skills.id"],
            name="fk_tenant_skill_edges_to_tenant_skill_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(from_skill_id IS NULL) <> (from_tenant_skill_id IS NULL)", name=op.f("ck_tenant_skill_edges_one_from")
        ),
        sa.CheckConstraint(
            "(to_skill_id IS NULL) <> (to_tenant_skill_id IS NULL)", name=op.f("ck_tenant_skill_edges_one_to")
        ),
        sa.CheckConstraint(KINDS, name=op.f("ck_tenant_skill_edges_kind")),
        sa.CheckConstraint(WEIGHT, name=op.f("ck_tenant_skill_edges_weight")),
        sa.CheckConstraint(
            "coalesce(from_skill_id, from_tenant_skill_id) <> coalesce(to_skill_id, to_tenant_skill_id)",
            name=op.f("ck_tenant_skill_edges_not_self"),
        ),
    )
    op.create_index("ix_tenant_skill_edges_tenant_id", t, ["tenant_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_tenant_skill_edges_refs ON tenant_skill_edges (tenant_id, "
        "coalesce(from_skill_id, from_tenant_skill_id), coalesce(to_skill_id, to_tenant_skill_id), kind)"
    )
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- skill_graph_settings
    t = "skill_graph_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("expansion_default", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("match_depth", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("min_weight", sa.Numeric(3, 2), server_default=sa.text("0.30"), nullable=False),
        sa.Column("inference_auto_suggest", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_skill_graph_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_skill_graph_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("match_depth BETWEEN 1 AND 2", name=op.f("ck_skill_graph_settings_match_depth")),
        sa.CheckConstraint(
            "min_weight > 0 AND min_weight <= 1", name=op.f("ck_skill_graph_settings_min_weight")
        ),
    )
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- platform seed (owner role)
    from app.services.skills.seed import sync_graph_seed

    sync_graph_seed(op.get_bind())


def downgrade() -> None:
    for t in ("skill_graph_settings", "tenant_skill_edges"):
        disable_org_rls(t)
        disable_rls(t)
    op.drop_table("skill_graph_settings")
    op.execute("DROP INDEX IF EXISTS uq_tenant_skill_edges_refs")
    op.drop_table("tenant_skill_edges")
    op.drop_table("skill_edges")
