# ruff: noqa: E501
"""Epic 71 (HRMS & Accounting Connectors, stories 71.1-71.3, spec A1): ``hrms_field_mappings``, ``hrms_sync_records``
and ``hrms_settings``. Tenant tables with RLS + org RLS (internal only).

Revision ID: 20261010_505_01_hrms_sync
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_505_01_hrms_sync"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_hrms_ledger",)
depends_on: str | Sequence[str] | None = None

TABLES = ("hrms_field_mappings", "hrms_sync_records", "hrms_settings")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "hrms_field_mappings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("hrms_field", sa.String(100), nullable=False),
        sa.Column("local_field", sa.String(60), nullable=True),
        sa.Column("constant", sa.String(200), nullable=True),
        sa.Column("transform", sa.String(20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("lookup", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("sensitive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("purpose", sa.String(500), nullable=True),
        sa.Column("enabled_by", sa.Uuid(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "installation_id", "legal_entity_id", "hrms_field", name="uq_hrms_field_mappings_field"),
        _fk("installation_id", "app_installations", "fk_hrms_field_mappings_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_hrms_field_mappings_legal_entity"),
        _fk("enabled_by", "users", "fk_hrms_field_mappings_enabled_by"),
        sa.CheckConstraint(_in("transform", ("none", "date_iso", "date_dmy", "upper", "lower", "lookup")), name=op.f("ck_hrms_field_mappings_transform")),
        sa.CheckConstraint("local_field IS NOT NULL OR constant IS NOT NULL", name=op.f("ck_hrms_field_mappings_source")),
        sa.CheckConstraint("NOT sensitive OR (purpose IS NOT NULL AND enabled_by IS NOT NULL)", name=op.f("ck_hrms_field_mappings_sensitive_purpose")),
    )

    t = "hrms_sync_records"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("include_override", sa.Boolean(), nullable=True),
        sa.Column("employee_ref", sa.String(200), nullable=True),
        sa.Column("employee_code", sa.String(60), nullable=True),
        sa.Column("pushed_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_push_kind", sa.String(10), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("blocked_fields", postgresql.ARRAY(sa.String(100)), server_default=sa.text("'{}'::varchar(100)[]"), nullable=False),
        sa.Column("skip_reason", sa.String(40), nullable=True),
        sa.Column("last_message", sa.String(500), nullable=True),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hrms_status", sa.String(10), nullable=True),
        sa.Column("hrms_exit_date", sa.Date(), nullable=True),
        sa.Column("last_pulled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mismatch", sa.String(12), server_default=sa.text("'none'"), nullable=False),
        sa.Column("mismatch_task_id", sa.Uuid(), nullable=True),
        sa.Column("field_hashes", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "installation_id", "placement_id", name="uq_hrms_sync_records_placement"),
        _fk("installation_id", "app_installations", "fk_hrms_sync_records_installation", "CASCADE"),
        _fk("placement_id", "placements", "fk_hrms_sync_records_placement", "CASCADE"),
        sa.CheckConstraint("last_push_kind IS NULL OR " + _in("last_push_kind", ("hire", "change", "exit")), name=op.f("ck_hrms_sync_records_last_push_kind")),
        sa.CheckConstraint(_in("status", ("pending", "blocked", "synced", "failed", "skipped")), name=op.f("ck_hrms_sync_records_status")),
        sa.CheckConstraint("hrms_status IS NULL OR " + _in("hrms_status", ("active", "exited", "pending", "unknown")), name=op.f("ck_hrms_sync_records_hrms_status")),
        sa.CheckConstraint(_in("mismatch", ("none", "status", "exit_date", "both")), name=op.f("ck_hrms_sync_records_mismatch")),
    )
    op.create_index("ix_hrms_sync_records_status", t, ["status"])
    op.create_index("ix_hrms_sync_records_candidate_id", t, ["candidate_id"])
    op.create_index("ix_hrms_sync_records_mismatch", t, ["mismatch"], postgresql_where=sa.text("mismatch <> 'none'"))

    t = "hrms_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("push_worker_types", postgresql.ARRAY(sa.String(20)), server_default=sa.text("'{employee}'::varchar(20)[]"), nullable=False),
        sa.Column("push_on_join", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("push_changes", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("push_exits", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("pull_hour_ist", sa.SmallInteger(), server_default=sa.text("6"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_hrms_settings_tenant_id"),
        sa.CheckConstraint("push_worker_types <@ ARRAY['employee','contractor']::varchar(20)[]", name=op.f("ck_hrms_settings_worker_types")),
        sa.CheckConstraint("pull_hour_ist BETWEEN 0 AND 23", name=op.f("ck_hrms_settings_pull_hour")),
    )
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
