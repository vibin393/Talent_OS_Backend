# ruff: noqa: E501
"""Epic 38 stories 38.2-38.5 (FR-192..FR-195): ALTER the ``talent_pools`` hub, plus ``talent_pool_shares``,
``talent_pool_members``, ``talent_pool_refreshes``, ``talent_pool_subscriptions`` and ``demand_pool_pins``.

Data step (decision 11): every P1 ``candidate_lists`` row becomes a private static Pool (same owner, name,
description, archive state; ``legacy_list_id`` set), its members are copied with ``source='legacy_list'`` and its
shares become ``talent_pool_shares``. Idempotent (keyed on ``legacy_list_id``). P1 tables are never modified, so
the downgrade loses nothing.

Revision ID: 20261006_302_02_pools
Revises: 20261006_302_01_segments
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_302_02_pools"
down_revision: str | tuple[str, ...] | None = "20261006_302_01_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_POOL_COLUMNS = (
    "last_diff",
    "refresh_due_at",
    "last_refreshed_at",
    "member_count",
    "legacy_list_id",
    "template_key",
    "team_id",
    "description",
)


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _base(table: str, *, timestamps: bool = True) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *(_ts() if timestamps else []),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _alter_pools() -> None:
    t = "talent_pools"
    op.add_column(t, sa.Column("description", sa.Text(), nullable=True))
    op.add_column(t, sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("template_key", sa.String(60), nullable=True))
    op.add_column(t, sa.Column("legacy_list_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("member_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column(t, sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("refresh_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("last_diff", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_talent_pools_segment_id_segments",
        t,
        "segments",
        ["tenant_id", "segment_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_talent_pools_team_id_teams",
        t,
        "teams",
        ["tenant_id", "team_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_talent_pools_legacy_list",
        t,
        "candidate_lists",
        ["tenant_id", "legacy_list_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_talent_pools_dynamic_segment"), t, "kind = 'static' OR segment_id IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_talent_pools_team_visibility"), t, "visibility <> 'team' OR team_id IS NOT NULL"
    )
    op.create_index(
        "uq_talent_pools_legacy_list_id",
        t,
        ["legacy_list_id"],
        unique=True,
        postgresql_where=sa.text("legacy_list_id IS NOT NULL"),
    )
    op.create_index(
        "ix_talent_pools_refresh_due_at",
        t,
        ["refresh_due_at"],
        postgresql_where=sa.text("refresh_due_at IS NOT NULL"),
    )


def _create_tables() -> None:
    t = "talent_pool_shares"
    op.create_table(
        t,
        *_base(t),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        _fk("fk_talent_pool_shares_pool_id", "pool_id", "talent_pools", "CASCADE"),
        _fk("fk_talent_pool_shares_user_id_users", "user_id", "users", "CASCADE"),
        _fk("fk_talent_pool_shares_team_id_teams", "team_id", "teams", "CASCADE"),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (team_id IS NULL)", name=op.f("ck_talent_pool_shares_exactly_one_target")
        ),
        sa.UniqueConstraint("pool_id", "user_id", name="uq_talent_pool_shares_pool_id_user_id"),
        sa.UniqueConstraint("pool_id", "team_id", name="uq_talent_pool_shares_pool_id_team_id"),
    )
    op.create_index("ix_talent_pool_shares_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "talent_pool_members"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("reason", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("added_refresh_id", sa.Uuid(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_refresh_id", sa.Uuid(), nullable=True),
        _fk("fk_talent_pool_members_pool_id", "pool_id", "talent_pools", "CASCADE"),
        _fk("fk_talent_pool_members_candidate_id", "candidate_id", "candidates", "CASCADE"),
        sa.CheckConstraint(
            "source IN ('manual', 'segment', 'legacy_list', 'workflow')",
            name=op.f("ck_talent_pool_members_source"),
        ),
    )
    op.create_index("ix_talent_pool_members_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_talent_pool_members_active",
        t,
        ["pool_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index("ix_talent_pool_members_candidate_id", t, ["candidate_id"])
    op.create_index("ix_talent_pool_members_added_refresh", t, ["pool_id", "added_refresh_id"])
    op.create_index("ix_talent_pool_members_removed_refresh", t, ["pool_id", "removed_refresh_id"])
    enable_rls(t)

    t = "talent_pool_refreshes"
    op.create_table(
        t,
        *_base(t),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), server_default=sa.text("'running'"), nullable=False),
        sa.Column("added", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("removed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("evaluated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _fk("fk_talent_pool_refreshes_pool_id", "pool_id", "talent_pools", "CASCADE"),
        sa.CheckConstraint(
            "trigger IN ('nightly', 'manual', 'skill_graph', 'created')",
            name=op.f("ck_talent_pool_refreshes_trigger"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed', 'skipped_needs_repair')",
            name=op.f("ck_talent_pool_refreshes_status"),
        ),
    )
    op.create_index("ix_talent_pool_refreshes_tenant_id", t, ["tenant_id"])
    op.create_index("ix_talent_pool_refreshes_pool_id", t, ["pool_id", "started_at"])
    enable_rls(t)

    t = "talent_pool_subscriptions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        _fk("fk_talent_pool_subscriptions_pool_id", "pool_id", "talent_pools", "CASCADE"),
        _fk("fk_talent_pool_subscriptions_user_id", "user_id", "users", "CASCADE"),
        sa.UniqueConstraint("pool_id", "user_id", name="uq_talent_pool_subscriptions_pool_id_user_id"),
    )
    op.create_index("ix_talent_pool_subscriptions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_talent_pool_subscriptions_user_id", t, ["user_id"])
    enable_rls(t)

    t = "demand_pool_pins"
    op.create_table(
        t,
        *_base(t),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("pinned_by", sa.Uuid(), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _fk("fk_demand_pool_pins_demand_id", "demand_id", "demands", "CASCADE"),
        _fk("fk_demand_pool_pins_pool_id", "pool_id", "talent_pools", "CASCADE"),
        _fk("fk_demand_pool_pins_pinned_by", "pinned_by", "users"),
        sa.UniqueConstraint("demand_id", "pool_id", name="uq_demand_pool_pins_demand_id_pool_id"),
    )
    op.create_index("ix_demand_pool_pins_tenant_id", t, ["tenant_id"])
    op.create_index("ix_demand_pool_pins_pool_id", t, ["pool_id"])
    enable_rls(t)


# Data step (idempotent, keyed on legacy_list_id). Runs as the migration owner role (RLS bypass).
_COPY_LISTS = """
INSERT INTO talent_pools (id, tenant_id, name, kind, owner_id, visibility, status, archived_at, description,
                          legacy_list_id, member_count, created_at, updated_at)
SELECT gen_random_uuid(), l.tenant_id, l.name, 'static', l.owner_id, 'private',
       CASE WHEN l.archived_at IS NULL THEN 'active' ELSE 'archived' END, l.archived_at, l.description,
       l.id, 0, l.created_at, l.updated_at
FROM candidate_lists l
WHERE NOT EXISTS (SELECT 1 FROM talent_pools p WHERE p.legacy_list_id = l.id)
"""
_COPY_MEMBERS = """
INSERT INTO talent_pool_members (id, tenant_id, pool_id, candidate_id, source, reason, added_by, added_at)
SELECT gen_random_uuid(), m.tenant_id, p.id, m.candidate_id, 'legacy_list',
       jsonb_build_object('legacy_list_id', m.list_id::text), m.added_by, m.created_at
FROM candidate_list_members m
JOIN talent_pools p ON p.legacy_list_id = m.list_id AND p.tenant_id = m.tenant_id
WHERE NOT EXISTS (
    SELECT 1 FROM talent_pool_members x
    WHERE x.pool_id = p.id AND x.candidate_id = m.candidate_id AND x.removed_at IS NULL
)
"""
_COPY_SHARES = """
INSERT INTO talent_pool_shares (id, tenant_id, pool_id, user_id, team_id, created_at, updated_at)
SELECT gen_random_uuid(), s.tenant_id, p.id, s.user_id, s.team_id, s.created_at, s.updated_at
FROM candidate_list_shares s
JOIN talent_pools p ON p.legacy_list_id = s.list_id AND p.tenant_id = s.tenant_id
WHERE NOT EXISTS (
    SELECT 1 FROM talent_pool_shares x
    WHERE x.pool_id = p.id AND x.user_id IS NOT DISTINCT FROM s.user_id AND x.team_id IS NOT DISTINCT FROM s.team_id
)
"""
_COUNT_MEMBERS = """
UPDATE talent_pools p SET member_count = (
    SELECT count(*) FROM talent_pool_members m WHERE m.pool_id = p.id AND m.removed_at IS NULL
)
WHERE p.legacy_list_id IS NOT NULL
"""


def upgrade() -> None:
    _alter_pools()
    _create_tables()
    for stmt in (_COPY_LISTS, _COPY_MEMBERS, _COPY_SHARES, _COUNT_MEMBERS):
        op.execute(stmt)


def downgrade() -> None:
    for t in (
        "demand_pool_pins",
        "talent_pool_subscriptions",
        "talent_pool_refreshes",
        "talent_pool_members",
        "talent_pool_shares",
    ):
        disable_rls(t)
        op.drop_table(t)
    t = "talent_pools"
    # Pools created from P1 lists are removed; the P1 lists themselves were never touched.
    op.execute("DELETE FROM talent_pools WHERE legacy_list_id IS NOT NULL")
    op.drop_index("ix_talent_pools_refresh_due_at", table_name=t)
    op.drop_index("uq_talent_pools_legacy_list_id", table_name=t)
    op.drop_constraint(op.f("ck_talent_pools_team_visibility"), t, type_="check")
    op.drop_constraint(op.f("ck_talent_pools_dynamic_segment"), t, type_="check")
    op.drop_constraint("fk_talent_pools_legacy_list", t, type_="foreignkey")
    op.drop_constraint("fk_talent_pools_team_id_teams", t, type_="foreignkey")
    op.drop_constraint("fk_talent_pools_segment_id_segments", t, type_="foreignkey")
    for col in _NEW_POOL_COLUMNS:
        op.drop_column(t, col)
