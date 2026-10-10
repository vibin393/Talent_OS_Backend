# ruff: noqa: E501
"""Epic 72 (Calendar Sync, stories 72.1-72.4, spec A1): ``calendar_channels``, ``calendar_event_links``,
``calendar_sync_settings``, ``calendar_free_busy_optouts`` (tenant tables, RLS + org RLS) and the platform lookup
``calendar_channel_lookups``. Widens ``ck_interview_participants_response_status`` with ``tentative`` (D-18).

Revision ID: 20261010_506_01_calendar_sync
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261010_506_01_calendar_sync"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_calendar_sync",)
depends_on: str | Sequence[str] | None = None

TABLES = ("calendar_channels", "calendar_event_links", "calendar_sync_settings", "calendar_free_busy_optouts")
PROVIDERS = ("google", "microsoft", "fake")
RESPONSES_OLD = ("needs_action", "accepted", "declined")
RESPONSES_NEW = ("needs_action", "accepted", "declined", "tentative")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(t: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False)


def _fk(col: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "calendar_channels"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(10), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider_resource_id", sa.String(300), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_token", sa.Text(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        sa.Column("renew_after", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "channel_id", name="uq_calendar_channels_tenant_id_channel_id"),
        _fk("connection_id", "user_connections", "fk_calendar_channels_connection_id_user_connections", "CASCADE"),
        _fk("user_id", "users", "fk_calendar_channels_user_id_users", "CASCADE"),
        sa.CheckConstraint(_in("provider", PROVIDERS), name=op.f("ck_calendar_channels_provider")),
        sa.CheckConstraint(_in("status", ("active", "renewing", "polling", "stopped", "failed")), name=op.f("ck_calendar_channels_status")),
    )
    op.create_index(
        "uq_calendar_channels_live", t, ["tenant_id", "connection_id"], unique=True,
        postgresql_where=sa.text("status IN ('active','renewing','polling')"),
    )  # fmt: skip
    op.create_index("ix_calendar_channels_renew", t, ["status", "renew_after"])
    op.create_index("ix_calendar_channels_poll", t, ["status", "next_poll_at"])

    t = "calendar_event_links"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(10), nullable=False),
        sa.Column("provider_event_id", sa.String(500), nullable=False),
        sa.Column("ical_uid", sa.String(200), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("tos_sequence", sa.Integer(), nullable=False),
        sa.Column("tos_tag", sa.String(100), nullable=False),
        sa.Column("tos_etag", sa.String(200), nullable=True),
        sa.Column("seen_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_status", sa.String(12), nullable=True),
        sa.Column("state", sa.String(10), server_default=sa.text("'in_sync'"), nullable=False),
        sa.Column("restore_count_window", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "connection_id", "provider_event_id", name="uq_calendar_event_links_event"),
        _fk("interview_id", "interviews", "fk_calendar_event_links_interview_id_interviews", "CASCADE"),
        _fk("participant_id", "interview_participants", "fk_calendar_event_links_participant_id_interview_participants", "CASCADE"),
        sa.CheckConstraint(_in("provider", PROVIDERS), name=op.f("ck_calendar_event_links_provider")),
        sa.CheckConstraint(_in("role", ("organizer", "attendee")), name=op.f("ck_calendar_event_links_role")),
        sa.CheckConstraint(_in("state", ("in_sync", "moved", "deleted", "restoring", "conflict", "detached")), name=op.f("ck_calendar_event_links_state")),
        sa.CheckConstraint(
            f"seen_status IS NULL OR {_in('seen_status', ('confirmed', 'tentative', 'cancelled', 'deleted'))}",
            name=op.f("ck_calendar_event_links_seen_status"),
        ),
    )
    op.create_index("ix_calendar_event_links_interview_id", t, ["interview_id"])
    op.create_index("ix_calendar_event_links_connection_ical", t, ["connection_id", "ical_uid"])

    t = "calendar_sync_settings"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("move_policy", sa.String(20), server_default=sa.text("'restore'"), nullable=False),
        sa.Column("poll_minutes", sa.SmallInteger(), server_default=sa.text("15"), nullable=False),
        sa.Column("restore_limit_per_hour", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("org_source_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_calendar_sync_settings_tenant_id"),
        sa.CheckConstraint(_in("move_policy", ("restore", "keep_as_proposal")), name=op.f("ck_calendar_sync_settings_move_policy")),
        sa.CheckConstraint("poll_minutes BETWEEN 5 AND 60", name=op.f("ck_calendar_sync_settings_poll_minutes")),
        sa.CheckConstraint("restore_limit_per_hour BETWEEN 1 AND 10", name=op.f("ck_calendar_sync_settings_restore_limit_per_hour")),
    )

    t = "calendar_free_busy_optouts"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        *_ts(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_calendar_free_busy_optouts_user"),
        _fk("user_id", "users", "fk_calendar_free_busy_optouts_user_id_users", "CASCADE"),
    )

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)

    op.create_table(
        "calendar_channel_lookups",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_calendar_channel_lookups_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_calendar_channel_lookups")),
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON calendar_channel_lookups TO {get_settings().db_app_user}")

    op.drop_constraint(op.f("ck_interview_participants_response_status"), "interview_participants", type_="check")
    op.create_check_constraint(
        op.f("ck_interview_participants_response_status"), "interview_participants", _in("response_status", RESPONSES_NEW)
    )


def downgrade() -> None:
    op.execute("UPDATE interview_participants SET response_status = 'needs_action' WHERE response_status = 'tentative'")
    op.drop_constraint(op.f("ck_interview_participants_response_status"), "interview_participants", type_="check")
    op.create_check_constraint(
        op.f("ck_interview_participants_response_status"), "interview_participants", _in("response_status", RESPONSES_OLD)
    )
    op.drop_table("calendar_channel_lookups")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
