# ruff: noqa: E501
"""Tasks, notifications, notification deliveries, preferences and digest settings (Epic 10, stories 10.2 /
10.3) (tenant-owned, RLS).

Revision ID: 20261001_102
Revises: 20261001_101
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_102"
down_revision: str | tuple[str, ...] | None = "20261001_101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "tasks",
    "notifications",
    "notification_deliveries",
    "notification_preferences",
    "notification_settings",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def _user_fk(table: str, column: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], ["users.tenant_id", "users.id"], name=f"fk_{table}_{column}_users", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_all_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("priority", sa.String(length=10), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=10), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "entity_type IS NULL OR " + _in("entity_type", ("client", "contact", "demand", "candidate", "application")),
            name=op.f("ck_tasks_entity_type"),
        ),
        sa.CheckConstraint("(entity_type IS NULL) = (entity_id IS NULL)", name=op.f("ck_tasks_entity_pair")),
        sa.CheckConstraint(_in("priority", ("low", "normal", "high", "urgent")), name=op.f("ck_tasks_priority")),
        sa.CheckConstraint(_in("status", ("open", "done", "cancelled")), name=op.f("ck_tasks_status")),
        sa.CheckConstraint(_in("source", ("manual", "workflow")), name=op.f("ck_tasks_source")),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name=op.f("ck_tasks_title_length")),
        _user_fk("tasks", "owner_id", "RESTRICT"),
        _user_fk("tasks", "created_by", "RESTRICT"),
        _tenant_fk("tasks"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("ix_tasks_tenant_id"), "tasks", ["tenant_id"], unique=False)
    op.create_index("ix_tasks_owner_status_due", "tasks", ["tenant_id", "owner_id", "status", "due_at"], unique=False)
    op.create_index("ix_tasks_entity", "tasks", ["tenant_id", "entity_type", "entity_id"], unique=False)
    op.create_index(
        "ix_tasks_reminder_due",
        "tasks",
        ["tenant_id", "reminder_at"],
        unique=False,
        postgresql_where=sa.text("status = 'open' AND reminded_at IS NULL"),
    )

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=True),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("link_path", sa.String(length=500), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("in_app", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _user_fk("notifications", "user_id", "CASCADE"),
        _tenant_fk("notifications"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "dedupe_key", name=op.f("uq_notifications_tenant_id_user_id_dedupe_key")
        ),
    )
    op.create_index(op.f("ix_notifications_tenant_id"), "notifications", ["tenant_id"], unique=False)
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["tenant_id", "user_id", sa.text("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL AND in_app"),
    )
    op.create_index(
        "ix_notifications_user_created", "notifications", ["tenant_id", "user_id", "created_at"], unique=False
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("email_message_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(_in("channel", ("email", "digest")), name=op.f("ck_notification_deliveries_channel")),
        sa.CheckConstraint(
            _in("status", ("queued", "sent", "skipped", "failed")), name=op.f("ck_notification_deliveries_status")
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        _tenant_fk("notification_deliveries"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        op.f("ix_notification_deliveries_tenant_id"), "notification_deliveries", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_notification_deliveries_notification_id", "notification_deliveries", ["notification_id"], unique=False
    )

    op.create_table(
        "notification_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            _in("channel", ("in_app", "email", "both", "off")), name=op.f("ck_notification_preferences_channel")
        ),
        _user_fk("notification_preferences", "user_id", "CASCADE"),
        _tenant_fk("notification_preferences"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "type", name=op.f("uq_notification_preferences_tenant_id_user_id_type")
        ),
    )
    op.create_index(
        op.f("ix_notification_preferences_tenant_id"), "notification_preferences", ["tenant_id"], unique=False
    )

    op.create_table(
        "notification_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("digest_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("digest_hour_local", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("last_digest_date", sa.Date(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("digest_hour_local BETWEEN 0 AND 23", name=op.f("ck_notification_settings_digest_hour")),
        _user_fk("notification_settings", "user_id", "CASCADE"),
        _tenant_fk("notification_settings"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name=op.f("pk_notification_settings")),
    )

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
