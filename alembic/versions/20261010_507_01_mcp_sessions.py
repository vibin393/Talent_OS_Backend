# ruff: noqa: E501, RUF028, ARG001
"""Epic 73 (Governed MCP Server, stories 73.1 / 73.6, spec A2): ``mcp_sessions``, ``mcp_access_holds`` and
``mcp_settings``. Tenant tables with RLS + org RLS; CHECK names through ``op.f``.

Revision ID: 20261010_507_01_mcp_sessions
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_507_01_mcp_sessions"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_mcp",)
depends_on: str | Sequence[str] | None = None

TABLES = ("mcp_sessions", "mcp_access_holds", "mcp_settings")
END_REASONS = (
    "client_closed", "idle", "kill_switch", "ai_kill_switch", "mcp_disabled", "uninstalled",
    "authorization_revoked", "user_deactivated", "bulk_read", "admin",
)  # fmt: skip


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(t: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False)


def _fk(t: str, col: str, target: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT")


def upgrade() -> None:
    t = "mcp_sessions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_family_id", sa.Uuid(), nullable=True),
        sa.Column("session_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("protocol_version", sa.String(12), nullable=False),
        sa.Column("client_name", sa.String(120), nullable=False),
        sa.Column("client_version", sa.String(40), nullable=True),
        sa.Column("status", sa.String(8), server_default=sa.text("'active'"), nullable=False),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.Column(c, sa.Integer(), server_default=sa.text("0"), nullable=False) for c in (
            "call_count", "proposal_count", "refusal_count", "violation_count", "redaction_count", "distinct_candidates",
        )),  # fmt: skip
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("session_hash", name="uq_mcp_sessions_session_hash"),
        _fk(t, "installation_id", "app_installations", "fk_mcp_sessions_installation_id_app_installations"),
        _fk(t, "user_id", "users", "fk_mcp_sessions_user_id_users"),
        sa.CheckConstraint(_in("status", ("active", "paused", "ended")), name=op.f("ck_mcp_sessions_status")),
        sa.CheckConstraint(_in("end_reason", END_REASONS), name=op.f("ck_mcp_sessions_end_reason")),
        sa.CheckConstraint("(status = 'ended') = (ended_at IS NOT NULL)", name=op.f("ck_mcp_sessions_ended")),
    )
    op.create_index("ix_mcp_sessions_user_started", t, ["tenant_id", "user_id", sa.text("started_at DESC")])
    op.create_index("ix_mcp_sessions_status", t, ["tenant_id", "status"])
    op.create_index("ix_mcp_sessions_run_id", t, ["run_id"])

    t = "mcp_access_holds"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(12), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("auto_release_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _fk(t, "user_id", "users", "fk_mcp_access_holds_user_id_users"),
        _fk(t, "session_id", "mcp_sessions", "fk_mcp_access_holds_session_id_mcp_sessions"),
        _fk(t, "released_by", "users", "fk_mcp_access_holds_released_by_users"),
        sa.CheckConstraint(_in("reason", ("bulk_read", "admin")), name=op.f("ck_mcp_access_holds_reason")),
    )
    op.create_index(
        "uq_mcp_access_holds_open", t, ["tenant_id", "user_id"], unique=True, postgresql_where=sa.text("released_at IS NULL")
    )

    t = "mcp_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_mcp_settings_tenant_id_tenants"), primary_key=True),
        *_ts(),
        sa.Column("calls_per_minute", sa.Integer(), nullable=True),
        sa.Column("calls_per_user_day", sa.Integer(), server_default=sa.text("2000"), nullable=False),
        sa.Column("proposals_per_hour", sa.Integer(), server_default=sa.text("25"), nullable=False),
        sa.Column("bulk_read_threshold", sa.Integer(), server_default=sa.text("500"), nullable=False),
        sa.Column("max_results", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("allowed_origins", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("notify_user_on_first_connection", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _fk(t, "updated_by", "users", "fk_mcp_settings_updated_by_users"),
        sa.CheckConstraint("calls_per_minute IS NULL OR calls_per_minute BETWEEN 5 AND 120", name=op.f("ck_mcp_settings_calls_per_minute")),
        sa.CheckConstraint("calls_per_user_day BETWEEN 100 AND 2000", name=op.f("ck_mcp_settings_calls_per_user_day")),
        sa.CheckConstraint("proposals_per_hour BETWEEN 1 AND 25", name=op.f("ck_mcp_settings_proposals_per_hour")),
        sa.CheckConstraint("bulk_read_threshold BETWEEN 50 AND 500", name=op.f("ck_mcp_settings_bulk_read_threshold")),
        sa.CheckConstraint("max_results BETWEEN 10 AND 50", name=op.f("ck_mcp_settings_max_results")),
    )
    for table in TABLES:
        if table != "mcp_settings":
            op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
