# ruff: noqa: E501
"""Epic 31 (Extension, Conversion & Offboarding) 218_01: ``placement_exit_settings``, ``placement_end_alerts``,
``placement_end_date_history`` and ``extensions`` (FR-162, FR-163).

One non-terminal extension per placement (partial UNIQUE). ``extensions.client_id`` / ``candidate_id`` are
inherited from the placement (``tos_inherit_org``) and drive org RLS for Client Portal reads. FKs only to the
contract-layer ``placements`` hub (200_07) and the P1 ``stored_files`` table (R2).

Revision ID: 20261005_218_01_exit_extensions
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_218_01_exit_extensions"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_placement_exit",)
depends_on: str | Sequence[str] | None = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def _placement_fk(table: str, name: str) -> sa.ForeignKeyConstraint:  # noqa: ARG001
    return sa.ForeignKeyConstraint(
        ["tenant_id", "placement_id"],
        ["placements.tenant_id", "placements.id"],
        name=name,
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "placement_exit_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_ts(),
        sa.Column(
            "alert_offsets_days",
            postgresql.JSONB(),
            server_default=sa.text("'[60, 30, 15]'::jsonb"),
            nullable=False,
        ),
        sa.Column("notify_client_users", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "bench_excluded_reasons",
            postgresql.JSONB(),
            server_default=sa.text('\'["converted", "misconduct", "worker_resignation"]\'::jsonb'),
            nullable=False,
        ),
        sa.Column("early_attrition_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("auto_case_grace_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_placement_exit_settings"),
        _tenant("placement_exit_settings"),
    )
    enable_rls("placement_exit_settings")

    op.create_table(
        "placement_end_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("offset_days", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_placement_end_alerts"),
        _tenant("placement_end_alerts"),
        _placement_fk("placement_end_alerts", "fk_placement_end_alerts_placement"),
        sa.UniqueConstraint(
            "tenant_id", "placement_id", "end_date", "offset_days", name="uq_placement_end_alerts_key"
        ),
    )
    op.create_index("ix_placement_end_alerts_tenant_id", "placement_end_alerts", ["tenant_id"])
    enable_rls("placement_end_alerts")

    op.create_table(
        "placement_end_date_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("old_end_date", sa.Date(), nullable=True),
        sa.Column("new_end_date", sa.Date(), nullable=True),
        sa.Column("cause", sa.String(30), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_placement_end_date_history"),
        _tenant("placement_end_date_history"),
        _placement_fk("placement_end_date_history", "fk_placement_end_date_history_placement"),
        sa.CheckConstraint(
            "cause IN ('extension', 'offboarding', 'cancel_offboarding')",
            name="ck_placement_end_date_history_cause",
        ),
    )
    op.create_index("ix_placement_end_date_history_tenant_id", "placement_end_date_history", ["tenant_id"])
    op.create_index("ix_placement_end_date_history_placement", "placement_end_date_history", ["placement_id"])
    enable_rls("placement_end_date_history")

    op.create_table(
        "extensions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_ts(),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("current_end_date", sa.Date(), nullable=False),
        sa.Column("new_end_date", sa.Date(), nullable=False),
        sa.Column("segment_effective_from", sa.Date(), nullable=True),
        sa.Column("rate_unit", sa.String(10), nullable=True),
        sa.Column("pay_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("bill_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("vendor_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("burden_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("po_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(30), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column("checks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("exception_approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("exception_status", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("exception_reason", sa.Text(), nullable=True),
        sa.Column("client_response_by_kind", sa.String(20), nullable=True),
        sa.Column("client_response_by_id", sa.Uuid(), nullable=True),
        sa.Column("client_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_evidence_file_id", sa.Uuid(), nullable=True),
        sa.Column("client_comment", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_segment_id", sa.Uuid(), nullable=True),
        sa.Column("withdraw_reason", sa.Text(), nullable=True),
        sa.Column("extension_letter_document_id", sa.Uuid(), nullable=True),
        sa.Column("send_extension_letter", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("proposed_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_extensions"),
        _tenant("extensions"),
        _placement_fk("extensions", "fk_extensions_placement"),
        sa.ForeignKeyConstraint(
            ["client_evidence_file_id"],
            ["stored_files.id"],
            name="fk_extensions_client_evidence_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_extensions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_extensions_tenant_id_human_id"),
        sa.CheckConstraint(
            "status IN ('proposed', 'pending_approval', 'pending_client_confirmation', 'confirmed', 'declined', 'withdrawn')",
            name="ck_extensions_status",
        ),
        sa.CheckConstraint(
            "exception_status IN ('none', 'pending', 'approved', 'rejected')",
            name="ck_extensions_exception_status",
        ),
        sa.CheckConstraint(
            "client_response_by_kind IS NULL OR client_response_by_kind IN ('client_user', 'secure_link', 'recorded_by_user', 'user')",
            name="ck_extensions_client_response_by_kind",
        ),
        sa.CheckConstraint("new_end_date > current_end_date", name="ck_extensions_dates"),
        sa.CheckConstraint(
            "(pay_rate IS NULL OR pay_rate >= 0) AND (bill_rate IS NULL OR bill_rate >= 0) AND (vendor_rate IS NULL OR vendor_rate >= 0)",
            name="ck_extensions_rates",
        ),
    )
    op.create_index("ix_extensions_tenant_id", "extensions", ["tenant_id"])
    op.create_index("ix_extensions_placement_id", "extensions", ["placement_id"])
    op.create_index(
        "uq_extensions_open",
        "extensions",
        ["tenant_id", "placement_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'pending_approval', 'pending_client_confirmation')"),
    )
    enable_rls("extensions")
    enable_org_rls(
        "extensions",
        client_col="client_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ("client_id", "candidate_id")),
    )


def downgrade() -> None:
    disable_org_rls("extensions")
    disable_rls("extensions")
    op.drop_table("extensions")
    for table in ("placement_end_date_history", "placement_end_alerts", "placement_exit_settings"):
        disable_rls(table)
        op.drop_table(table)
