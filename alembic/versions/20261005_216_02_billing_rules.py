"""Epic 29 216_02: ``billing_rules``, ``billing_schedule_items``, ``manual_billing_items``, ``invoice_runs``,
``invoice_run_exceptions`` (FR-143, FR-145, FR-147). Internal-only org RLS.

Revision ID: 20261005_216_02_billing_rules
Revises: 20261005_216_01_billing_tax
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_216_02_billing_rules"
down_revision: str | tuple[str, ...] | None = "20261005_216_01_billing_tax"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_rules",
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.String(length=15), server_default=sa.text("'hourly'"), nullable=False),
        sa.Column("prorate", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "proration_days", sa.String(length=10), server_default=sa.text("'working'"), nullable=False
        ),
        sa.Column("billable_categories", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "expense_rebill", sa.String(length=12), server_default=sa.text("'at_cost'"), nullable=False
        ),
        sa.Column("markup_pct", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column(
            "grouping", sa.String(length=20), server_default=sa.text("'per_placement'"), nullable=False
        ),
        sa.Column("frequency", sa.String(length=15), server_default=sa.text("'monthly'"), nullable=False),
        sa.Column("line_template", sa.String(length=300), nullable=True),
        sa.Column("sac_code", sa.String(length=8), server_default=sa.text("'998513'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "basis IN ('hourly', 'daily', 'monthly_fixed', 'milestone', 'fixed_fee')",
            name=op.f("ck_billing_rules_basis"),
        ),
        sa.CheckConstraint(
            "expense_rebill IN ('at_cost', 'markup', 'pure_agent')",
            name=op.f("ck_billing_rules_expense_rebill"),
        ),
        sa.CheckConstraint(
            "frequency IN ('weekly', 'monthly', 'on_milestone')", name=op.f("ck_billing_rules_frequency")
        ),
        sa.CheckConstraint(
            "grouping IN ('per_placement', 'per_po', 'per_address_period')",
            name=op.f("ck_billing_rules_grouping"),
        ),
        sa.CheckConstraint(
            "proration_days IN ('working', 'calendar')", name=op.f("ck_billing_rules_proration_days")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_billing_rules_placement_id_placements",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_billing_rules_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_rules")),
        sa.UniqueConstraint(
            "tenant_id", "placement_id", name=op.f("uq_billing_rules_tenant_id_placement_id")
        ),
    )
    op.create_index(op.f("ix_billing_rules_tenant_id"), "billing_rules", ["tenant_id"], unique=False)
    op.create_table(
        "billing_schedule_items",
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('monthly_fixed', 'scheduled_fee', 'conversion_fee')",
            name=op.f("ck_billing_schedule_items_kind"),
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_billing_schedule_items_amount")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_billing_schedule_items_placement_id_placements",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_billing_schedule_items_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_schedule_items")),
        sa.UniqueConstraint(
            "tenant_id",
            "placement_id",
            "kind",
            "period_start",
            name=op.f("uq_billing_schedule_items_tenant_id_placement_id_kind_period_start"),
        ),
    )
    op.create_index(
        op.f("ix_billing_schedule_items_tenant_id"), "billing_schedule_items", ["tenant_id"], unique=False
    )
    op.create_table(
        "manual_billing_items",
        sa.Column("kind", sa.String(length=15), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("offer_id", sa.Uuid(), nullable=True),
        sa.Column("item_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("sac_code", sa.String(length=8), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("rate", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("po_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('one_off', 'placement_fee', 'milestone')", name=op.f("ck_manual_billing_items_kind")
        ),
        sa.CheckConstraint("quantity > 0 AND rate >= 0", name=op.f("ck_manual_billing_items_amounts")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_manual_billing_items_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_manual_billing_items_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_manual_billing_items_placement_id_placements",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_manual_billing_items_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_billing_items")),
    )
    op.create_index(
        op.f("ix_manual_billing_items_tenant_id"), "manual_billing_items", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_manual_billing_items_offer",
        "manual_billing_items",
        ["tenant_id", "offer_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'placement_fee'"),
    )
    op.create_table(
        "invoice_runs",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("placement_ids", postgresql.ARRAY(sa.Uuid()), nullable=True),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("progress_pct", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("draft_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "totals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'building', 'built', 'failed', 'discarded')",
            name=op.f("ck_invoice_runs_status"),
        ),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_invoice_runs_period")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_invoice_runs_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_invoice_runs_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_invoice_runs_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_runs")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_invoice_runs_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_invoice_runs_tenant_id_human_id")),
    )
    op.create_index(op.f("ix_invoice_runs_tenant_id"), "invoice_runs", ["tenant_id"], unique=False)
    op.create_table(
        "invoice_run_exceptions",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=10), server_default=sa.text("'error'"), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["invoice_runs.tenant_id", "invoice_runs.id"],
            name="fk_invoice_run_exceptions_run_id_invoice_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_invoice_run_exceptions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_run_exceptions")),
    )
    op.create_index("ix_invoice_run_exceptions_run_id", "invoice_run_exceptions", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_invoice_run_exceptions_tenant_id"), "invoice_run_exceptions", ["tenant_id"], unique=False
    )
    enable_rls("billing_rules")
    enable_rls("billing_schedule_items")
    enable_rls("manual_billing_items")
    enable_rls("invoice_runs")
    enable_rls("invoice_run_exceptions")
    enable_org_rls("billing_rules")
    enable_org_rls("billing_schedule_items")
    enable_org_rls("manual_billing_items")
    enable_org_rls("invoice_runs")
    enable_org_rls("invoice_run_exceptions")


def downgrade() -> None:
    disable_org_rls("invoice_run_exceptions")
    disable_rls("invoice_run_exceptions")
    op.drop_table("invoice_run_exceptions")
    disable_org_rls("invoice_runs")
    disable_rls("invoice_runs")
    op.drop_table("invoice_runs")
    disable_org_rls("manual_billing_items")
    disable_rls("manual_billing_items")
    op.drop_table("manual_billing_items")
    disable_org_rls("billing_schedule_items")
    disable_rls("billing_schedule_items")
    op.drop_table("billing_schedule_items")
    disable_org_rls("billing_rules")
    disable_rls("billing_rules")
    op.drop_table("billing_rules")
