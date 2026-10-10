# ruff: noqa: E501
"""Epic 47 (FR-239, FR-240): report schedules and the report run log.

``report_runs`` records every preview / run / export / scheduled delivery; ``uq_report_runs_slot`` makes a
scheduled slot idempotent per recipient. Builder files live on ``report_runs.file_id`` (the P1 ``exports`` table
is not altered).

Revision ID: 20261006_311_02_report_sched
Revises: 20261006_311_01_report_defs
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_311_02_report_sched"
down_revision: str | tuple[str, ...] | None = "20261006_311_01_report_defs"
branch_labels: str | Sequence[str] | None = None
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
    t = "report_schedules"
    op.create_table(
        t,
        *_base(t),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("cadence", sa.String(10), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("month_day", sa.SmallInteger(), nullable=True),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        sa.Column("format", sa.String(5), nullable=False),
        sa.Column("recipient_user_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("paused_reason", sa.String(40), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        _fk("fk_report_schedules_report_id", "report_id", "report_definitions", "CASCADE"),
        _fk("fk_report_schedules_owner_id_users", "owner_id", "users"),
        _ck(t, "cadence", "cadence IN ('daily', 'weekly', 'monthly')"),
        _ck(t, "format", "format IN ('xlsx', 'csv', 'pdf')"),
        _ck(t, "status", "status IN ('active', 'paused')"),
        _ck(
            t,
            "weekday",
            "(cadence = 'weekly') = (weekday IS NOT NULL) AND (weekday IS NULL OR weekday BETWEEN 0 AND 6)",
        ),
        _ck(
            t,
            "month_day",
            "(cadence = 'monthly') = (month_day IS NOT NULL)"
            " AND (month_day IS NULL OR month_day = -1 OR month_day BETWEEN 1 AND 28)",
        ),
        _ck(t, "recipients", "cardinality(recipient_user_ids) BETWEEN 1 AND 50"),
        _ck(t, "failures", "consecutive_failures >= 0"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_report_id", t, ["report_id"])
    op.create_index(f"ix_{t}_due", t, ["status", "next_run_at"])
    enable_rls(t)

    t = "report_runs"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("slot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_kind", sa.String(10), nullable=False),
        sa.Column("principal_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'running'"), nullable=False),
        sa.Column("limit_kind", sa.String(10), nullable=True),
        sa.Column("skip_reason", sa.String(40), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("masked_fields", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("format", sa.String(5), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("delivery", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _fk("fk_report_runs_report_id", "report_id", "report_definitions", "CASCADE"),
        _fk("fk_report_runs_schedule_id", "schedule_id", "report_schedules", "SET NULL"),
        _fk("fk_report_runs_principal_user_id_users", "principal_user_id", "users"),
        _fk("fk_report_runs_file_id_stored_files", "file_id", "stored_files", "SET NULL"),
        _ck(t, "run_kind", "run_kind IN ('preview', 'run', 'export', 'scheduled')"),
        _ck(t, "status", "status IN ('queued', 'running', 'done', 'failed', 'limit_hit', 'skipped')"),
        _ck(t, "limit_kind", "limit_kind IS NULL OR limit_kind IN ('row_cap', 'timeout')"),
        _ck(t, "format", "format IS NULL OR format IN ('xlsx', 'csv', 'pdf')"),
        _ck(t, "delivery", "delivery IN ('attachment', 'link', 'none')"),
        _ck(t, "limit_hit", "(status = 'limit_hit') = (limit_kind IS NOT NULL)"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_report_id", t, ["report_id", "started_at"])
    op.create_index(f"ix_{t}_principal", t, ["principal_user_id", "started_at"])
    op.create_index(f"ix_{t}_expires", t, ["expires_at"])
    op.create_index(
        "uq_report_runs_slot",
        t,
        ["schedule_id", "slot_at", "principal_user_id"],
        unique=True,
        postgresql_where=sa.text("schedule_id IS NOT NULL"),
    )
    enable_rls(t)


def downgrade() -> None:
    for t in ("report_runs", "report_schedules"):
        disable_rls(t)
        op.drop_table(t)
