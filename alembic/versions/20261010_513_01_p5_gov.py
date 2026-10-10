# ruff: noqa: E501
"""Epic 79 (Phase 5 Governance, stories 79.2 / 79.5, spec A4 / A7 / A10): access-review and DSR downstream tables.

Tenant tables (RLS + internal-only org RLS on the access-review tables; RLS only on ``dsr_downstream_items`` which is a
DPO-only record): ``access_review_settings``, ``access_reviews``, ``access_review_items``, ``dsr_downstream_items``.
Also widens the ``provider_connector_configs`` category CHECK with ``calendar`` (Epic 72 installs org calendar adapters
through the Epic 69 marketplace -- coordinator Ask First, p5_followups).

Revision ID: 20261010_513_01_p5_gov
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_513_01_p5_gov"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_governance",)
depends_on: str | Sequence[str] | None = None

REVIEW_STATUSES = ("open", "completed", "expired")
ITEM_KINDS = ("installation_grant", "user_authorization", "api_key")
ITEM_DECISIONS = ("pending", "confirmed", "reduced", "revoked", "auto_revoked")
DOWNSTREAM_CHANNELS = ("webhook", "connector", "listed_only")
DOWNSTREAM_STATUSES = ("sent", "confirmed", "failed", "manual_required", "statutory_retained")

# provider_connector_configs categories after Epic 72's marketplace-install path (Epic 66 500_01 list + calendar).
CONNECTOR_CATEGORIES = (
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp",
    "assessment", "dsc", "tds_statement", "fx", "voice", "job_board", "hrms", "stt",
    "calendar",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _stamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    # ---------------------------------------------------------------- access_review_settings (tenant, one row)
    t = "access_review_settings"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("cadence_months", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("stale_after_days", sa.SmallInteger(), server_default=sa.text("90"), nullable=False),
        sa.Column("auto_revoke_stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reviewer_role_key", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=f"fk_{t}_updated_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", name=f"uq_{t}_tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _ck(t, "cadence_months", "cadence_months BETWEEN 1 AND 12"),
        _ck(t, "stale_after_days", "stale_after_days BETWEEN 1 AND 3650"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- access_reviews (tenant)
    t = "access_reviews"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decided_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["completed_by"], ["users.id"], name=f"fk_{t}_completed_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _ck(t, "status", _in("status", REVIEW_STATUSES)),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_tenant_status", t, ["tenant_id", "status"])
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- access_review_items (tenant)
    t = "access_review_items"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("decision", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "review_id"], ["access_reviews.tenant_id", "access_reviews.id"],
            name=f"fk_{t}_review_id_access_reviews", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=f"fk_{t}_decided_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "review_id", "kind", "subject_id", name=f"uq_{t}_review_subject"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _ck(t, "kind", _in("kind", ITEM_KINDS)),
        _ck(t, "decision", _in("decision", ITEM_DECISIONS)),
    )
    op.create_index(f"ix_{t}_tenant_review", t, ["tenant_id", "review_id"])
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- dsr_downstream_items (tenant; DPO-only record)
    t = "dsr_downstream_items"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("dsr_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _ck(t, "channel", _in("channel", DOWNSTREAM_CHANNELS)),
        _ck(t, "status", _in("status", DOWNSTREAM_STATUSES)),
    )
    op.create_index(f"ix_{t}_tenant_dsr", t, ["tenant_id", "dsr_id"])
    enable_rls(t)

    # ---------------------------------------------------------------- widen connector category CHECK with 'calendar'
    # Raw SQL by exact constraint name (op.drop_constraint re-applies the metadata naming convention and mangles it).
    op.execute("ALTER TABLE provider_connector_configs DROP CONSTRAINT ck_provider_connector_configs_category")
    op.execute(
        "ALTER TABLE provider_connector_configs ADD CONSTRAINT ck_provider_connector_configs_category "
        f"CHECK ({_in('category', CONNECTOR_CATEGORIES)})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE provider_connector_configs DROP CONSTRAINT ck_provider_connector_configs_category")
    op.execute(
        "ALTER TABLE provider_connector_configs ADD CONSTRAINT ck_provider_connector_configs_category "
        f"CHECK ({_in('category', tuple(c for c in CONNECTOR_CATEGORIES if c != 'calendar'))})"
    )
    for t in ("dsr_downstream_items",):
        disable_rls(t)
        op.drop_table(t)
    for t in ("access_review_items", "access_reviews", "access_review_settings"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
