# ruff: noqa: E501
"""Epic 38 story 38.1 (FR-192, AD-42): ``segments``, ``segment_versions`` and ``talent_pool_settings``.

First revision of branch ``p3_pools``. **Epic 39 depends on this revision id** (``audiences`` FK ``segments``).
Definitions are semantic-layer JSON (``core.semantic``); no person data. Tenant RLS only.

Revision ID: 20261006_302_01_segments
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_302_01_segments"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_pools",)
depends_on: str | Sequence[str] | None = None


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    t = "talent_pool_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "nightly_refresh_local_time", sa.String(5), server_default=sa.text("'02:00'"), nullable=False
        ),
        sa.Column("digest_local_time", sa.String(5), server_default=sa.text("'08:00'"), nullable=False),
        sa.Column("max_dynamic_pools", sa.Integer(), server_default=sa.text("2000"), nullable=False),
        sa.Column("last_nightly_on", sa.String(10), nullable=True),
        sa.Column("last_digest_on", sa.String(10), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_talent_pool_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_talent_pool_settings_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "max_dynamic_pools BETWEEN 1 AND 100000", name=op.f("ck_talent_pool_settings_max_dynamic")
        ),
        sa.CheckConstraint(
            "nightly_refresh_local_time ~ '^[0-2][0-9]:[0-5][0-9]$'",
            name=op.f("ck_talent_pool_settings_nightly_time"),
        ),
        sa.CheckConstraint(
            "digest_local_time ~ '^[0-2][0-9]:[0-5][0-9]$'", name=op.f("ck_talent_pool_settings_digest_time")
        ),
    )
    op.create_index("ix_talent_pool_settings_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "segments"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_key", sa.String(60), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("definition_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("visibility", sa.String(10), server_default=sa.text("'private'"), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("needs_repair", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("repair_issues", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_segments_tenant_id_id"),
        _fk("fk_segments_owner_id_users", "owner_id", "users"),
        _fk("fk_segments_team_id_teams", "team_id", "teams"),
        sa.CheckConstraint(
            "visibility IN ('private', 'team', 'tenant')", name=op.f("ck_segments_visibility")
        ),
        sa.CheckConstraint(
            "visibility <> 'team' OR team_id IS NOT NULL", name=op.f("ck_segments_team_visibility")
        ),
        sa.CheckConstraint("definition_version >= 1", name=op.f("ck_segments_version")),
    )
    op.create_index("ix_segments_tenant_id", t, ["tenant_id"])
    op.create_index("ix_segments_owner_id", t, ["owner_id"])
    enable_rls(t)

    t = "segment_versions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _fk("fk_segment_versions_segment_id_segments", "segment_id", "segments", "CASCADE"),
        sa.UniqueConstraint("segment_id", "version", name="uq_segment_versions_segment_id_version"),
    )
    op.create_index("ix_segment_versions_tenant_id", t, ["tenant_id"])
    enable_rls(t)


def downgrade() -> None:
    for t in ("segment_versions", "segments", "talent_pool_settings"):
        disable_rls(t)
        op.drop_table(t)
