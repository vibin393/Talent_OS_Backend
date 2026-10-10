"""Epic 5 embeddings (AI service, AD-7): ``candidate_embeddings`` and ``demand_embeddings`` (``float4[]``).

When the ``vector`` extension is *available* on the server, the extension is created and a ``vec vector(384)``
column plus an HNSW ``vector_cosine_ops`` index are added to both tables (``PgVectorIndex``). Local PG18
without pgvector skips this and the application uses ``ArrayVectorIndex``.

Revision ID: 20261001_52
Revises: 20261001_51
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_52"
down_revision: str | tuple[str, ...] | None = "20261001_51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIM = 384


def _pgvector_available() -> bool:
    bind = op.get_bind()
    return bind.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).first() is not None


def upgrade() -> None:
    op.create_table(
        "candidate_embeddings",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), server_default=sa.text("'profile'"), nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.REAL()), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('profile')", name=op.f("ck_candidate_embeddings_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_embeddings_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_candidate_embeddings_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("candidate_id", "kind", name=op.f("pk_candidate_embeddings")),
    )
    op.create_index(op.f("ix_candidate_embeddings_tenant_id"), "candidate_embeddings", ["tenant_id"])
    op.create_index(
        "ix_candidate_embeddings_tenant_id_updated_at", "candidate_embeddings", ["tenant_id", "updated_at"]
    )
    enable_rls("candidate_embeddings")

    op.create_table(
        "demand_embeddings",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.REAL()), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_embeddings_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_demand_embeddings_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("demand_id", name=op.f("pk_demand_embeddings")),
    )
    op.create_index(op.f("ix_demand_embeddings_tenant_id"), "demand_embeddings", ["tenant_id"])
    enable_rls("demand_embeddings")

    if _pgvector_available():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        for table in ("candidate_embeddings", "demand_embeddings"):
            op.execute(f"ALTER TABLE {table} ADD COLUMN vec vector({DIM})")
            op.execute(f"CREATE INDEX ix_{table}_vec_hnsw ON {table} USING hnsw (vec vector_cosine_ops)")


def downgrade() -> None:
    for table in ("demand_embeddings", "candidate_embeddings"):
        disable_rls(table)
        op.drop_table(table)
