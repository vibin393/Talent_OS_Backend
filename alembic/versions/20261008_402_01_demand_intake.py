# ruff: noqa: E501, ARG001
"""Epic 55 story 55.1 (FR-281): demand intake -- ``demand_agent_settings``, the platform table
``demand_intake_routes`` (non-RLS; DELETE / TRUNCATE revoked from the app role), ``demand_intake_sender_domains`` and
``demand_intake_messages``.

Tenant tables: tenant RLS + internal-only org RLS (nothing here is portal-visible).

Revision ID: 20261008_402_01_demand_intake
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261008_402_01_demand_intake"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_demand_agent",)
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("demand_agent_settings", "demand_intake_sender_domains", "demand_intake_messages")
INTAKE_MODES = ("off", "connected_mailbox", "platform_address")
SOURCES = ("platform_address", "connected_mailbox", "upload", "paste")
STATUSES = (
    "received", "blocked", "unmatched", "ambiguous", "matched", "processing", "drafted", "no_roles", "failed",
    "discarded",
)  # fmt: skip
AUTH_RESULTS = ("pass", "fail", "unknown")
MATCH_METHODS = ("contact_email", "verified_domain", "manual", "forwarded_contact")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _fk(
    table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _stamps() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def upgrade() -> None:
    t = "demand_agent_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("intake_mode", sa.String(20), server_default=sa.text("'off'"), nullable=False),
        sa.Column("mailbox_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "mailbox_folder", sa.String(100), server_default=sa.text("'Talent OS Intake'"), nullable=False
        ),
        sa.Column("mailbox_cursor", sa.String(100), nullable=True),
        sa.Column("mailbox_status", sa.String(30), nullable=True),
        sa.Column("fallback_owner_id", sa.Uuid(), nullable=True),
        sa.Column("accept_forwarded", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("require_sender_auth", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("roles_per_message_max", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("duplicate_threshold", sa.Numeric(3, 2), server_default=sa.text("0.72"), nullable=False),
        sa.Column("duplicate_window_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("sla_risk_probability", sa.Numeric(3, 2), server_default=sa.text("0.60"), nullable=False),
        sa.Column("sla_scan_owner_id", sa.Uuid(), nullable=True),
        sa.Column("sla_digest_max_items", sa.Integer(), server_default=sa.text("25"), nullable=False),
        sa.Column("jd_template_md", sa.Text(), nullable=True),
        sa.Column("ack_contact_on_intake", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("route_token", sa.String(24), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk(t, "mailbox_user_id", "users", "fk_demand_agent_settings_mailbox_user_id_users"),
        _fk(t, "fallback_owner_id", "users", "fk_demand_agent_settings_fallback_owner_id_users"),
        _fk(t, "sla_scan_owner_id", "users", "fk_demand_agent_settings_sla_scan_owner_id_users"),
        _fk(t, "updated_by", "users", "fk_demand_agent_settings_updated_by_users"),
        _ck(t, "intake_mode", _in("intake_mode", INTAKE_MODES)),
        _ck(t, "roles_max", "roles_per_message_max BETWEEN 1 AND 5"),
        _ck(t, "dup_threshold", "duplicate_threshold BETWEEN 0.50 AND 0.95"),
        _ck(t, "dup_window", "duplicate_window_days BETWEEN 7 AND 365"),
        _ck(t, "sla_probability", "sla_risk_probability BETWEEN 0.30 AND 0.95"),
        _ck(t, "digest_max", "sla_digest_max_items BETWEEN 5 AND 100"),
    )

    # Platform lookup (no PII): platform-address token -> tenant.
    op.create_table(
        "demand_intake_routes",
        sa.Column("token", sa.String(24), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token", name="pk_demand_intake_routes"),
    )
    op.create_index("ix_demand_intake_routes_tenant_id", "demand_intake_routes", ["tenant_id"])
    role = get_settings().db_app_user
    op.execute(f"REVOKE DELETE, TRUNCATE ON demand_intake_routes FROM {role}")

    t = "demand_intake_sender_domains"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("domain", postgresql.CITEXT(), nullable=False),
        sa.Column("verified_by", sa.Uuid(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "domain", "client_id", name="uq_demand_intake_sender_domains_domain"
        ),
        _fk(t, "client_id", "clients", "fk_demand_intake_sender_domains_client_id_clients"),
        _fk(t, "verified_by", "users", "fk_demand_intake_sender_domains_verified_by_users"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])

    t = "demand_intake_messages"
    op.create_table(
        t,
        *_base(t),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("external_ref", sa.String(300), nullable=True),
        sa.Column("from_address", postgresql.CITEXT(), nullable=True),
        sa.Column("from_name", sa.String(200), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_file_id", sa.Uuid(), nullable=True),
        sa.Column("body_text_file_id", sa.Uuid(), nullable=True),
        sa.Column("body_preview", sa.String(500), nullable=True),
        sa.Column("attachments", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("auth_result", sa.String(20), nullable=True),
        sa.Column("forwarded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("match_method", sa.String(20), nullable=True),
        sa.Column("matched_client_id", sa.Uuid(), nullable=True),
        sa.Column("matched_contact_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'received'"), nullable=False),
        sa.Column("status_reason", sa.String(60), nullable=True),
        sa.Column("extraction", postgresql.JSONB(), nullable=True),
        sa.Column("extraction_ai_action_id", sa.Uuid(), nullable=True),
        sa.Column(
            "draft_demand_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("group_label", sa.String(120), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_kind", sa.String(20), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "raw_file_id", "stored_files", "fk_demand_intake_messages_raw_file_id_stored_files"),
        _fk(t, "body_text_file_id", "stored_files", "fk_demand_intake_messages_body_file_stored_files"),
        _fk(t, "forwarded_by_user_id", "users", "fk_demand_intake_messages_forwarded_by_users"),
        _fk(t, "matched_client_id", "clients", "fk_demand_intake_messages_matched_client_id_clients"),
        _fk(t, "matched_contact_id", "contacts", "fk_demand_intake_messages_matched_contact_id_contacts"),
        _fk(t, "owner_user_id", "users", "fk_demand_intake_messages_owner_user_id_users"),
        _ck(t, "source", _in("source", SOURCES)),
        _ck(t, "status", _in("status", STATUSES)),
        _ck(t, "auth_result", f"auth_result IS NULL OR {_in('auth_result', AUTH_RESULTS)}"),
        _ck(t, "match_method", f"match_method IS NULL OR {_in('match_method', MATCH_METHODS)}"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_demand_intake_messages_external_ref", t, ["tenant_id", "source", "external_ref"], unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )  # fmt: skip
    op.create_index(
        "ix_demand_intake_messages_status", t, ["tenant_id", "status", sa.text("received_at DESC")]
    )
    op.create_index("ix_demand_intake_messages_matched_client_id", t, ["matched_client_id"])

    for table in TENANT_TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    op.drop_table("demand_intake_routes")
