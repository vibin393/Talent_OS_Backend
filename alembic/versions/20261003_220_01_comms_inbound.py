# ruff: noqa: E501
"""Phase 2 Epic 33 (FR-171): inbound email capture and channel settings.

``email_threads`` (provider threads started from Talent OS whose replies are synced), ``inbound_emails``
(captured replies, text only) and ``comms_channel_settings`` (one row per tenant). Tenant RLS + internal-only
org RLS on all three. First revision of branch ``p2_comms``.

Revision ID: 20261003_220_01_comms_inbound
Revises: 20261002_200_07
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_220_01_comms_inbound"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_comms",)
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("comms_channel_settings", "email_threads", "inbound_emails")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "comms_channel_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sms_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("whatsapp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("default_sms_sender_id", sa.String(length=6), nullable=True),
        sa.Column("dlt_entity_id", sa.String(length=30), nullable=True),
        sa.Column("quiet_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inbound_sync_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("thread_sync_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("thread_sync_days BETWEEN 7 AND 90", name=op.f("ck_comms_channel_settings_thread_sync_days")),
        _tenant_fk("comms_channel_settings"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comms_channel_settings")),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_comms_channel_settings_tenant_id")),
    )
    op.create_table(
        "email_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email_message_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("thread_ref", sa.String(length=255), nullable=False),
        sa.Column("rfc_message_id", sa.String(length=500), nullable=True),
        sa.Column("related", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("sync_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_ref", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'stopped', 'error')", name=op.f("ck_email_threads_status")),
        sa.ForeignKeyConstraint(
            ["email_message_id"], ["email_messages.id"], name=op.f("fk_email_threads_email_message_id_email_messages"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["user_connections.id"], name=op.f("fk_email_threads_connection_id_user_connections"), ondelete="CASCADE"
        ),
        _tenant_fk("email_threads"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_threads")),
        sa.UniqueConstraint(
            "tenant_id", "provider", "thread_ref", "user_id", name=op.f("uq_email_threads_tenant_id_provider_thread_ref_user_id")
        ),
    )
    op.create_index("ix_email_threads_active", "email_threads", ["tenant_id", "status", "sync_until"])
    op.create_index("ix_email_threads_email_message_id", "email_threads", ["email_message_id"])
    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("email_message_id", sa.Uuid(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("from_address", postgresql.CITEXT(), nullable=False),
        sa.Column("subject", sa.String(length=500), server_default=sa.text("''"), nullable=False),
        sa.Column("body_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attachment_names", postgresql.ARRAY(sa.String(length=255)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("related", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["email_threads.id"], name=op.f("fk_inbound_emails_thread_id_email_threads"), ondelete="CASCADE"
        ),
        _tenant_fk("inbound_emails"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_emails")),
        sa.UniqueConstraint("tenant_id", "provider_message_id", name=op.f("uq_inbound_emails_tenant_id_provider_message_id")),
    )
    op.create_index("ix_inbound_emails_thread_id", "inbound_emails", ["thread_id"])
    op.create_index("ix_inbound_emails_related", "inbound_emails", ["related"], postgresql_using="gin")
    for table in TABLES:
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
    op.drop_table("inbound_emails")
    op.drop_table("email_threads")
    op.drop_table("comms_channel_settings")
