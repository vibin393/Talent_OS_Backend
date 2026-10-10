"""Skill catalog (story 3.2): platform ``skills`` / ``skill_aliases`` (no RLS, SELECT-only for the runtime
role, seeded from ``skills_seed.yaml``), tenant skills / aliases and ``candidate_skills`` (RLS).

Revision ID: 20261001_32
Revises: 20261001_31
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import enable_rls
from app.core.settings import get_settings
from app.services.candidates.skill_seed import sync_seed

revision: str = "20261001_32"
down_revision: str | None = "20261001_31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_TABLES = ("skills", "skill_aliases")


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["skills.id"], name=op.f("fk_skills_parent_id_skills"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint("slug", name=op.f("uq_skills_slug")),
    )
    op.create_index(op.f("ix_skills_parent_id"), "skills", ["parent_id"], unique=False)

    op.create_table(
        "skill_aliases",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("alias_norm", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name=op.f("fk_skill_aliases_skill_id_skills"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_aliases")),
        sa.UniqueConstraint("alias_norm", name=op.f("uq_skill_aliases_alias_norm")),
    )
    op.create_index(op.f("ix_skill_aliases_skill_id"), "skill_aliases", ["skill_id"], unique=False)

    op.create_table(
        "tenant_skills",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("parent_skill_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_from", sa.String(length=30), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name=op.f("ck_tenant_skills_status")
        ),
        sa.ForeignKeyConstraint(
            ["parent_skill_id"],
            ["skills.id"],
            name=op.f("fk_tenant_skills_parent_skill_id_skills"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_skills_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_skills")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_tenant_skills_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "slug", name=op.f("uq_tenant_skills_tenant_id_slug")),
    )
    op.create_index(op.f("ix_tenant_skills_tenant_id"), "tenant_skills", ["tenant_id"], unique=False)
    enable_rls("tenant_skills")

    op.create_table(
        "tenant_skill_aliases",
        sa.Column("alias_norm", sa.String(length=200), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_skill_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(skill_id IS NULL) <> (tenant_skill_id IS NULL)",
            name=op.f("ck_tenant_skill_aliases_exactly_one_skill"),
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_tenant_skill_aliases_skill_id_skills"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_skill_id"],
            ["tenant_skills.tenant_id", "tenant_skills.id"],
            name="fk_tenant_skill_aliases_tenant_skill_id_tenant_skills",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_skill_aliases_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_skill_aliases")),
        sa.UniqueConstraint(
            "tenant_id", "alias_norm", name=op.f("uq_tenant_skill_aliases_tenant_id_alias_norm")
        ),
    )
    op.create_index(
        op.f("ix_tenant_skill_aliases_tenant_id"), "tenant_skill_aliases", ["tenant_id"], unique=False
    )
    enable_rls("tenant_skill_aliases")

    op.create_table(
        "candidate_skills",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_skill_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("last_used", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("source IN ('parsed', 'manual')", name=op.f("ck_candidate_skills_source")),
        sa.CheckConstraint(
            "(skill_id IS NULL) <> (tenant_skill_id IS NULL)",
            name=op.f("ck_candidate_skills_exactly_one_skill"),
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name=op.f("fk_candidate_skills_skill_id_skills"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_skills_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_skill_id"],
            ["tenant_skills.tenant_id", "tenant_skills.id"],
            name="fk_candidate_skills_tenant_skill_id_tenant_skills",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_skills_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_skills")),
        sa.UniqueConstraint(
            "candidate_id", "skill_id", name=op.f("uq_candidate_skills_candidate_id_skill_id")
        ),
        sa.UniqueConstraint(
            "candidate_id", "tenant_skill_id", name=op.f("uq_candidate_skills_candidate_id_tenant_skill_id")
        ),
    )
    op.create_index("ix_candidate_skills_skill_id", "candidate_skills", ["skill_id"], unique=False)
    op.create_index(op.f("ix_candidate_skills_tenant_id"), "candidate_skills", ["tenant_id"], unique=False)
    op.create_index(
        "ix_candidate_skills_tenant_skill_id", "candidate_skills", ["tenant_skill_id"], unique=False
    )
    enable_rls("candidate_skills")

    # Platform catalog: the runtime role may only read it (default privileges granted full DML).
    role = get_settings().db_app_user
    for table in PLATFORM_TABLES:
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {role}")
        op.execute(f"GRANT SELECT ON {table} TO {role}")
    # Idempotent upsert by slug (re-sync later with ``python -m app.cli seed-skills``).
    sync_seed(op.get_bind())


def downgrade() -> None:
    op.drop_table("candidate_skills")
    op.drop_table("tenant_skill_aliases")
    op.drop_table("tenant_skills")
    op.drop_table("skill_aliases")
    op.drop_table("skills")
