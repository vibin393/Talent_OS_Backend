# ruff: noqa: E501
"""Epic 16 203_03: rate cards, rate card lines and rate checks (FR-90, FR-91).

Published / superseded cards of one client + scope never overlap (EXCLUDE gist on the effective date range);
cards and their lines are immutable outside ``draft`` (only the supersede / archive transitions may touch
``effective_to`` / ``archived_at``). ``rate_checks`` is internal-only.

Revision ID: 20261003_203_03_rate_cards
Revises: 20261003_203_02_contracts
Create Date: 2026-10-03
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261003_203_03_rate_cards"
down_revision: str | tuple[str, ...] | None = "20261003_203_02_contracts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


def base_columns(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]
    if timestamps:
        cols += [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        ]
    return cols


def tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "rate_cards",
        *base_columns("rate_cards"),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("is_template", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_template_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'published', 'superseded', 'archived')", name="ck_rate_cards_status"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_rate_cards_effective_range"),
        sa.CheckConstraint("(client_id IS NULL) = is_template", name="ck_rate_cards_template_has_no_client"),
        sa.CheckConstraint("contract_id IS NULL OR client_id IS NOT NULL", name="ck_rate_cards_contract_needs_client"),
        sa.CheckConstraint(
            "status IN ('draft', 'archived') OR is_template OR effective_from IS NOT NULL",
            name="ck_rate_cards_published_has_start",
        ),
        tfk("rate_cards", "client_id", "clients"),
        tfk("rate_cards", "contract_id", "contracts"),
        tfk("rate_cards", "source_template_id", "rate_cards"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_rate_cards_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_rate_cards_tenant_id_human_id"),
    )
    op.create_index("ix_rate_cards_tenant_id", "rate_cards", ["tenant_id"])
    op.create_index("ix_rate_cards_client_id", "rate_cards", ["client_id"])
    op.execute(
        f"CREATE UNIQUE INDEX uq_rate_cards_version ON rate_cards (tenant_id, client_id, COALESCE(contract_id, {ZERO}), version) "
        "WHERE client_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE rate_cards ADD CONSTRAINT ex_rate_cards_no_overlap EXCLUDE USING gist ("
        f"tenant_id WITH =, client_id WITH =, (COALESCE(contract_id, {ZERO})) WITH =, "
        "daterange(effective_from, COALESCE(effective_to, 'infinity'::date), '[]') WITH &&"
        ") WHERE (status IN ('published', 'superseded') AND client_id IS NOT NULL)"
    )

    op.create_table(
        "rate_card_lines",
        *base_columns("rate_card_lines"),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("rate_card_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(200), nullable=False),
        sa.Column("role_key", sa.String(200), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("location_key", sa.String(200), nullable=True),
        sa.Column("experience_min_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("experience_max_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("min_bill_rate", sa.Numeric(14, 4), nullable=False),
        sa.Column("max_bill_rate", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("rate_unit", sa.String(10), nullable=False),
        sa.Column("max_vendor_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("markup_pct", sa.Numeric(7, 4), nullable=True),
        sa.CheckConstraint("min_bill_rate >= 0 AND min_bill_rate <= max_bill_rate", name="ck_rate_card_lines_bill_range"),
        sa.CheckConstraint(
            "experience_min_years IS NULL OR experience_max_years IS NULL OR experience_min_years <= experience_max_years",
            name="ck_rate_card_lines_experience_range",
        ),
        sa.CheckConstraint("rate_unit IN ('hour', 'day', 'month', 'year', 'fixed')", name="ck_rate_card_lines_rate_unit"),
        tfk("rate_card_lines", "rate_card_id", "rate_cards", "CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_rate_card_lines_tenant_id_id"),
    )
    op.create_index("ix_rate_card_lines_tenant_id", "rate_card_lines", ["tenant_id"])
    op.create_index("ix_rate_card_lines_rate_card_id", "rate_card_lines", ["rate_card_id"])

    op.create_table(
        "rate_checks",
        *base_columns("rate_checks", timestamps=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("input_hash", sa.CHAR(64), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("rate_card_id", sa.Uuid(), nullable=True),
        sa.Column("rate_card_line_id", sa.Uuid(), nullable=True),
        sa.Column("min_bill_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("max_bill_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("max_vendor_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("exception_status", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "subject_kind IN ('demand', 'vendor_release', 'submission', 'offer', 'placement', 'extension', 'rate_segment')",
            name="ck_rate_checks_subject_kind",
        ),
        sa.CheckConstraint(
            "result IN ('ok', 'out_of_range', 'no_matching_line', 'vendor_rate_exceeded', 'not_validated')",
            name="ck_rate_checks_result",
        ),
        sa.CheckConstraint("exception_status IN ('none', 'pending', 'approved', 'rejected')", name="ck_rate_checks_exception_status"),
        tfk("rate_checks", "client_id", "clients"),
    )
    op.create_index("ix_rate_checks_tenant_id", "rate_checks", ["tenant_id"])
    op.create_index("ix_rate_checks_subject", "rate_checks", ["tenant_id", "subject_kind", "subject_id", "checked_at"])

    for table in ("rate_cards", "rate_card_lines", "rate_checks"):
        enable_rls(table)
    # Rate cards are commercial terms: internal-only (the client portal reads none of them).
    enable_org_rls("rate_cards")
    enable_org_rls("rate_card_lines")
    enable_org_rls("rate_checks")
    enable_immutability(
        "rate_cards",
        status_column="status",
        mutable_states=("draft",),
        allowed_transitions=(("published", "superseded"), ("published", "archived"), ("superseded", "archived")),
        transition_columns=("effective_to", "archived_at"),
    )
    enable_immutability(
        "rate_card_lines",
        status_column=None,
        parent=ParentRef("rate_cards", "rate_card_id", "status", ("draft",)),
    )


def downgrade() -> None:
    disable_immutability("rate_card_lines")
    disable_immutability("rate_cards")
    for table in ("rate_checks", "rate_card_lines", "rate_cards"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
