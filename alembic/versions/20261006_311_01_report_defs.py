"""Epic 47 (FR-237, FR-238): report definitions, versions, shares, pins and per-tenant builder settings.

First revision of branch ``p3_reports`` (band 311) on the Phase 3 foundation head. Every table is tenant-owned
(``enable_rls``); no subject data is stored (definitions name registered semantic keys only).

Revision ID: 20261006_311_01_report_defs
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_311_01_report_defs"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_reports",)
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def _base(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
    ]
    if timestamps:
        cols += [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        ]
    return [
        *cols,
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, expr: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expr, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = "report_builder_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("preview_rows", sa.Integer(), server_default=sa.text("500"), nullable=False),
        sa.Column("export_row_cap", sa.Integer(), server_default=sa.text("100000"), nullable=False),
        sa.Column("statement_timeout_s", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("attachment_max_mb", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("link_ttl_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("max_recipients", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("max_schedules", sa.Integer(), server_default=sa.text("500"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _ck(t, "preview_rows", "preview_rows BETWEEN 1 AND 500"),
        _ck(t, "export_row_cap", "export_row_cap BETWEEN 1 AND 100000"),
        _ck(t, "timeout", "statement_timeout_s BETWEEN 1 AND 30"),
        _ck(t, "attachment_max_mb", "attachment_max_mb BETWEEN 1 AND 25"),
        _ck(t, "link_ttl_days", "link_ttl_days BETWEEN 1 AND 30"),
        _ck(t, "max_recipients", "max_recipients BETWEEN 1 AND 50"),
        _ck(t, "max_schedules", "max_schedules BETWEEN 1 AND 500"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "report_definitions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("definition_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("viz", postgresql.JSONB(), nullable=False),
        sa.Column("visibility", sa.String(10), server_default=sa.text("'private'"), nullable=False),
        sa.Column("status", sa.String(15), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("repair_issues", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("repair_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _fk("fk_report_definitions_owner_id_users", "owner_id", "users"),
        _ck(t, "visibility", "visibility IN ('private', 'shared')"),
        _ck(t, "status", "status IN ('ok', 'needs_repair')"),
        _ck(t, "version", "definition_version >= 1"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_owner_id", t, ["owner_id"])
    op.create_index(f"ix_{t}_source_key", t, ["tenant_id", "source_key"])
    enable_rls(t)

    t = "report_definition_versions"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("viz", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        _fk("fk_report_definition_versions_report_id", "report_id", "report_definitions"),
        _fk("fk_report_definition_versions_created_by_users", "created_by", "users"),
        sa.UniqueConstraint("report_id", "version", name="uq_report_definition_versions_report_version"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "report_shares"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(10), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        _fk("fk_report_shares_report_id", "report_id", "report_definitions", "CASCADE"),
        _fk("fk_report_shares_team_id_teams", "team_id", "teams", "CASCADE"),
        _fk("fk_report_shares_role_id_roles", "role_id", "roles", "CASCADE"),
        _fk("fk_report_shares_created_by_users", "created_by", "users"),
        _ck(t, "target_kind", "target_kind IN ('team', 'role', 'tenant')"),
        _ck(
            t,
            "target",
            "(target_kind = 'team' AND team_id IS NOT NULL AND role_id IS NULL)"
            " OR (target_kind = 'role' AND role_id IS NOT NULL AND team_id IS NULL)"
            " OR (target_kind = 'tenant' AND team_id IS NULL AND role_id IS NULL)",
        ),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_report_shares_target",
        t,
        [
            "report_id",
            "target_kind",
            sa.text("COALESCE(team_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            sa.text("COALESCE(role_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
    )
    enable_rls(t)

    t = "report_pins"
    op.create_table(
        t,
        *_base(t),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("viz_override", postgresql.JSONB(), nullable=True),
        _fk("fk_report_pins_user_id_users", "user_id", "users", "CASCADE"),
        _fk("fk_report_pins_report_id", "report_id", "report_definitions", "CASCADE"),
        sa.UniqueConstraint("user_id", "report_id", name="uq_report_pins_user_report"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)


def downgrade() -> None:
    for t in (
        "report_pins",
        "report_shares",
        "report_definition_versions",
        "report_definitions",
        "report_builder_settings",
    ):
        disable_rls(t)
        op.drop_table(t)
