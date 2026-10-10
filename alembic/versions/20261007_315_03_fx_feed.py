# ruff: noqa: E501
"""Phase 3 Epic 51 (FR-255) 315_03: live FX feed.

* ``fx_rates`` gains ``origin`` (``manual|feed``, default ``manual``) and the unique key is widened to
  ``uq_fx_rates_pair_date_origin (tenant_id, from_currency, to_currency, rate_date, origin)`` (coordinator
  decision 2), so a manual and a feed rate can coexist for one date. Manual rows are never updated by the feed.
* ``fx_feed_settings`` (one row per tenant) and ``fx_feed_runs`` UNIQUE (tenant, on_date, provider_key).

Downgrade removes the feed rows (they are re-fetchable; manual rows are kept) so the original key can be restored.

Revision ID: 20261007_315_03_fx_feed
Revises: 20261007_315_02_tds_recon
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_315_03_fx_feed"
down_revision: str | tuple[str, ...] | None = "20261007_315_02_tds_recon"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_UQ = "uq_fx_rates_tenant_id_from_currency_to_currency_rate_date"
NEW_UQ = "uq_fx_rates_pair_date_origin"


def upgrade() -> None:
    # ---------------------------------------------------------------- fx_rates.origin
    op.add_column(
        "fx_rates", sa.Column("origin", sa.String(10), server_default=sa.text("'manual'"), nullable=False)
    )
    op.create_check_constraint(op.f("ck_fx_rates_origin"), "fx_rates", "origin IN ('manual', 'feed')")
    op.drop_constraint(OLD_UQ, "fx_rates", type_="unique")
    op.create_unique_constraint(
        NEW_UQ, "fx_rates", ["tenant_id", "from_currency", "to_currency", "rate_date", "origin"]
    )

    # ---------------------------------------------------------------- fx_feed_settings
    t = "fx_feed_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "pairs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{USD/INR,EUR/INR,GBP/INR}'::text[]"),
            nullable=False,
        ),
        sa.Column("precedence", sa.String(12), server_default=sa.text("'manual_first'"), nullable=False),
        sa.Column("max_stale_days", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("fetch_hour_ist", sa.SmallInteger(), server_default=sa.text("10"), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_alert_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_fx_feed_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_fx_feed_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "precedence IN ('manual_first', 'feed_first')", name=op.f("ck_fx_feed_settings_precedence")
        ),
        sa.CheckConstraint("max_stale_days BETWEEN 1 AND 7", name=op.f("ck_fx_feed_settings_max_stale_days")),
        sa.CheckConstraint(
            "fetch_hour_ist BETWEEN 0 AND 23", name=op.f("ck_fx_feed_settings_fetch_hour_ist")
        ),
    )
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- fx_feed_runs
    t = "fx_feed_runs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("provider_key", sa.String(40), nullable=False),
        sa.Column("fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped_manual", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_fx_feed_runs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_fx_feed_runs_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "on_date", "provider_key", name="uq_fx_feed_runs_date_provider"),
        sa.CheckConstraint("status IN ('ok', 'partial', 'failed')", name=op.f("ck_fx_feed_runs_status")),
    )
    op.create_index("ix_fx_feed_runs_tenant_on_date", t, ["tenant_id", "on_date"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("fx_feed_runs", "fx_feed_settings"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
    op.execute("DELETE FROM fx_rates WHERE origin = 'feed'")
    op.drop_constraint(NEW_UQ, "fx_rates", type_="unique")
    op.create_unique_constraint(
        OLD_UQ, "fx_rates", ["tenant_id", "from_currency", "to_currency", "rate_date"]
    )
    op.drop_constraint(op.f("ck_fx_rates_origin"), "fx_rates", type_="check")
    op.drop_column("fx_rates", "origin")
