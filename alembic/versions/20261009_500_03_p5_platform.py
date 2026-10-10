# ruff: noqa: E501, RUF028
"""Phase 5 foundation (Epic 66, stories 66.4..66.10): platform controls, the Secrets Vault, the outbox, connector runtime
state, exposures and metering (spec 0.2).

Platform: ``platform_ecosystem_controls`` (+ row ``id=1``; SELECT / UPDATE only for ``talent_app``),
``platform_secrets``.
Tenant (RLS + internal-only org RLS): ``vault_data_keys``, ``vault_secrets``, ``vault_access_log``, ``outbox_events``,
``connector_sync_state``, ``connector_links``, ``subject_exposures``, ``usage_counters`` and ``api_request_log``
(RANGE-partitioned by month on ``at``; the current month + 3 ahead; every partition carries the same policies).

Revision ID: 20261009_500_03_p5_platform
Revises: 20261009_500_02_p5_oauth
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    create_monthly_partitions,
    disable_org_rls,
    disable_rls,
    enable_org_rls,
    enable_rls,
)
from app.core.settings import get_settings

revision: str = "20261009_500_03_p5_platform"
down_revision: str | tuple[str, ...] | None = "20261009_500_02_p5_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "vault_data_keys", "vault_secrets", "vault_access_log", "outbox_events", "connector_sync_state", "connector_links",
    "subject_exposures", "usage_counters",
)  # fmt: skip
OWNER_TYPES = (
    "installation",
    "provider_config",
    "webhook_subscription",
    "calendar_connection",
    "oauth_app_tenant",
)
USAGE_UNITS = (
    "api_request", "webhook_attempt", "mcp_tool_call", "connector_job", "job_board_posting", "telephony_minute",
    "transcription_minute", "portal_copilot_question",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_base(table: str, *, stamps: bool = True) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *(_stamps() if stamps else []),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _inst_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "installation_id"],
        ["app_installations.tenant_id", "app_installations.id"],
        name=f"fk_{table}_installation_id_inst",
        ondelete="CASCADE",
    )


def _jsonb(name: str, default: str = "{}") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    role = get_settings().db_app_user

    # ---------------------------------------------------------------- platform
    t = "platform_ecosystem_controls"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        _ts("global_suspended_at"),
        _uuid("global_suspended_by"),
        sa.Column("global_suspended_reason", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _ck(t, "singleton", "id = 1"),
    )
    op.execute("INSERT INTO platform_ecosystem_controls (id) VALUES (1)")
    op.execute(f"REVOKE INSERT, DELETE, TRUNCATE ON {t} FROM {role}")
    op.execute(f"GRANT SELECT, UPDATE ON {t} TO {role}")

    t = "platform_secrets"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("kek_id", sa.String(60), nullable=False),
        sa.Column("fingerprint", sa.String(4), nullable=True),
        _ts("rotated_at"),
        _uuid("created_by"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("name", name="uq_platform_secrets_name"),
    )

    # ---------------------------------------------------------------- vault
    t = "vault_data_keys"
    op.create_table(
        t,
        *_tenant_base(t),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("kek_id", sa.String(60), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        _ts("rewrapped_at"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_vault_data_keys_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_vault_data_keys_tenant_id_version"),
        _ck(t, "status", "status IN ('active', 'retired')"),
    )
    op.create_index(
        "uq_vault_data_keys_active",
        t,
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    t = "vault_secrets"
    op.create_table(
        t,
        *_tenant_base(t),
        sa.Column("owner_type", sa.String(30), nullable=False),
        _uuid("owner_id", nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("dek_version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(4), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        _jsonb("meta"),
        _ts("expires_at"),
        _ts("rotated_at"),
        sa.Column("created_by_kind", sa.String(20), nullable=True),
        _uuid("created_by_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_vault_secrets_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "owner_type", "owner_id", "name", name="uq_vault_secrets_owner_name"
        ),
        _ck(t, "owner_type", _in("owner_type", OWNER_TYPES)),
    )
    op.create_index("ix_vault_secrets_owner", t, ["owner_type", "owner_id"])
    op.create_index("ix_vault_secrets_expires_at", t, ["expires_at"])

    t = "vault_access_log"
    op.create_table(
        t,
        *_tenant_base(t, stamps=False),
        _uuid("secret_id", nullable=False),
        sa.Column("owner_type", sa.String(30), nullable=False),
        _uuid("owner_id", nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("purpose", sa.String(60), nullable=False),
        sa.Column("caller", sa.String(120), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_vault_access_log_secret_id_at", t, ["secret_id", "at"])
    op.create_index("ix_vault_access_log_at", t, ["at"])

    # ---------------------------------------------------------------- outbox
    t = "outbox_events"
    op.create_table(
        t,
        *_tenant_base(t),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("event_version", sa.SmallInteger(), nullable=False),
        sa.Column("visibility", sa.String(10), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        _uuid("subject_id", nullable=False),
        _ts("occurred_at", nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("source_event", sa.String(80), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=True),
        _uuid("app_installation_id"),
        sa.Column("dispatch_status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        _ts("dispatched_at"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_outbox_events_tenant_id_id"),
        sa.UniqueConstraint("seq", name="uq_outbox_events_seq"),
        _ck(t, "visibility", "visibility IN ('public', 'internal')"),
        _ck(t, "dispatch_status", "dispatch_status IN ('pending', 'dispatched')"),
    )
    op.create_index("ix_outbox_events_pending", t, ["tenant_id", "dispatch_status", "seq"])
    op.create_index("ix_outbox_events_type", t, ["tenant_id", "event_type", "occurred_at"])
    op.create_index("ix_outbox_events_subject", t, ["subject_type", "subject_id", "seq"])
    op.create_index("ix_outbox_events_occurred_at", t, ["occurred_at"])

    # ---------------------------------------------------------------- connector runtime
    t = "connector_sync_state"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        _uuid("config_id"),
        sa.Column("job_key", sa.String(60), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _jsonb("cursor"),
        _ts("next_run_at"),
        _ts("last_run_at"),
        _ts("last_success_at"),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        _ts("lock_until"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_connector_sync_state_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "installation_id",
            "config_id",
            "job_key",
            name="uq_connector_sync_state_job",
            postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
        _inst_fk(t),
    )
    op.create_index(
        "ix_connector_sync_state_next_run_at", t, ["next_run_at"], postgresql_where=sa.text("enabled")
    )

    t = "connector_links"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        _uuid("local_id", nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("local_version", sa.String(100), nullable=True),
        sa.Column("external_version", sa.String(100), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'linked'"), nullable=False),
        _ts("last_synced_at"),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        _uuid("candidate_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_connector_links_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "installation_id", "entity_type", "local_id", name="uq_connector_links_local"
        ),
        sa.UniqueConstraint(
            "tenant_id", "installation_id", "entity_type", "external_id", name="uq_connector_links_external"
        ),
        _inst_fk(t),
        _ck(t, "status", "status IN ('linked', 'pending', 'conflict', 'error', 'unlinked')"),
    )
    op.create_index("ix_connector_links_candidate_id", t, ["candidate_id"])

    t = "subject_exposures"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id"),
        _uuid("api_key_id"),
        sa.Column("subject_type", sa.String(40), nullable=False),
        _uuid("subject_id", nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        _ts("first_at", nullable=False),
        _ts("last_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_subject_exposures_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "installation_id",
            "api_key_id",
            "subject_type",
            "subject_id",
            "channel",
            name="uq_subject_exposures_subject_channel",
            postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
        _inst_fk(t),
        sa.ForeignKeyConstraint(
            ["tenant_id", "api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_subject_exposures_api_key_id_api_keys",
            ondelete="CASCADE",
        ),  # fmt: skip
        _ck(t, "channel", "channel IN ('api', 'webhook', 'connector', 'mcp')"),
        _ck(t, "recipient", "(installation_id IS NULL) <> (api_key_id IS NULL)"),
    )
    op.create_index("ix_subject_exposures_api_key_id", t, ["api_key_id"])
    op.create_index("ix_subject_exposures_subject", t, ["subject_type", "subject_id"])

    # ---------------------------------------------------------------- metering
    t = "usage_counters"
    op.create_table(
        t,
        *_tenant_base(t),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        _uuid("installation_id"),
        _uuid("api_key_id"),
        _uuid("app_id"),
        sa.Column("route_group", sa.String(60), server_default=sa.text("''"), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 3), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_usage_counters_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "day",
            "unit",
            "installation_id",
            "api_key_id",
            "route_group",
            name="uq_usage_counters_key",
            postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
        _ck(t, "unit", _in("unit", USAGE_UNITS)),
    )
    op.create_index("ix_usage_counters_app_id_day", t, ["app_id", "day"])

    for table in TENANT_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)

    t = "api_request_log"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _ts("at", nullable=False),
        _uuid("installation_id"),
        _uuid("api_key_id"),
        _uuid("token_id"),
        _uuid("user_id"),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("route_template", sa.String(300), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("api_version", sa.String(10), nullable=False),
        sa.Column("deprecated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", "at", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _ck(t, "caller", "installation_id IS NOT NULL OR api_key_id IS NOT NULL"),
        postgresql_partition_by="RANGE (at)",
    )
    op.create_index("ix_api_request_log_tenant_id", t, ["tenant_id"])
    op.create_index("ix_api_request_log_installation_id_at", t, ["installation_id", "at"])
    op.create_index("ix_api_request_log_api_key_id_at", t, ["api_key_id", "at"])
    op.create_index("ix_api_request_log_at", t, ["at"])
    enable_rls(t)
    enable_org_rls(t)
    create_monthly_partitions(t, months_ahead=3)


def downgrade() -> None:
    op.drop_table("api_request_log")  # drops every partition with it
    for table in reversed(TENANT_TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    op.drop_table("platform_secrets")
    op.drop_table("platform_ecosystem_controls")
