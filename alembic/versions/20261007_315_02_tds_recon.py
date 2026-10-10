# ruff: noqa: E501
"""Phase 3 Epic 51 (FR-254) 315_02: 26AS / AIS TDS reconciliation.

Runs are keyed by Legal Entity (PAN) + FY + quarter (coordinator decision 7); deducting Clients are identified by
``tds_deductor_tans`` (TAN -> Client). Reconciliation writes only these tables, never ``tds_deductions`` /
``tds_deduction_reversals``.

* ``tds_deductor_tans`` UNIQUE (tenant, tan);
* ``tds_recon_runs`` (status parsing|ready|failed|superseded);
* ``tds_statement_lines`` (``line_hash`` = sha256 of the normalised line, used to carry decisions over on
  supersede);
* ``tds_recon_matches``: ``active`` is false once the run is superseded. Partial UNIQUE: one open
  (proposed|confirmed|unmatched_ack) active match per statement line; one active ``confirmed`` match per deduction.

RLS on every new table + internal-only org policy.

Revision ID: 20261007_315_02_tds_recon
Revises: 20261007_315_01_dsc
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_315_02_tds_recon"
down_revision: str | tuple[str, ...] | None = "20261007_315_01_dsc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("tds_recon_matches", "tds_statement_lines", "tds_recon_runs", "tds_deductor_tans")


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(table: str, column: str, target: str, name: str | None = None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    t = "tds_deductor_tans"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("tan", sa.CHAR(10), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _fk(t, "client_id", "clients"),
        sa.UniqueConstraint("tenant_id", "tan", name="uq_tds_deductor_tans_tenant_id_tan"),
        sa.CheckConstraint("tan ~ '^[A-Z]{4}[0-9]{5}[A-Z]$'", name=op.f("ck_tds_deductor_tans_tan_format")),
    )
    op.create_index("ix_tds_deductor_tans_client_id", t, ["client_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "tds_recon_runs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("fy", sa.CHAR(7), nullable=False),
        sa.Column("quarter", sa.CHAR(2), nullable=False),
        sa.Column("statement_kind", sa.String(5), nullable=False),
        sa.Column("source", sa.String(10), server_default=sa.text("'upload'"), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'parsing'"), nullable=False),
        sa.Column("line_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "parse_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("assessment_year", sa.String(9), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "legal_entity_id", "legal_entities"),
        _fk(t, "file_id", "stored_files"),
        sa.CheckConstraint("fy ~ '^[0-9]{4}-[0-9]{2}$'", name=op.f("ck_tds_recon_runs_fy_format")),
        sa.CheckConstraint("quarter IN ('Q1', 'Q2', 'Q3', 'Q4')", name=op.f("ck_tds_recon_runs_quarter")),
        sa.CheckConstraint(
            "statement_kind IN ('26as', 'ais')", name=op.f("ck_tds_recon_runs_statement_kind")
        ),
        sa.CheckConstraint("source IN ('upload', 'connector')", name=op.f("ck_tds_recon_runs_source")),
        sa.CheckConstraint(
            "status IN ('parsing', 'ready', 'failed', 'superseded')", name=op.f("ck_tds_recon_runs_status")
        ),
    )
    op.create_index("ix_tds_recon_runs_le_fy_quarter", t, ["tenant_id", "legal_entity_id", "fy", "quarter"])
    enable_rls(t)
    enable_org_rls(t)

    t = "tds_statement_lines"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("line_ref", sa.String(100), nullable=False),
        sa.Column("deductor_tan", sa.CHAR(10), nullable=False),
        sa.Column("deductor_name", sa.String(200), nullable=False),
        sa.Column("section", sa.String(10), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("amount_paid", sa.Numeric(16, 2), nullable=False),
        sa.Column("tds_deducted", sa.Numeric(16, 2), nullable=False),
        sa.Column("tds_deposited", sa.Numeric(16, 2), nullable=True),
        sa.Column("booking_status", sa.CHAR(1), nullable=True),
        sa.Column("line_hash", sa.CHAR(64), nullable=False),
        _fk(t, "run_id", "tds_recon_runs"),
    )
    op.create_index("ix_tds_statement_lines_run_id", t, ["run_id"])
    op.create_index("ix_tds_statement_lines_line_hash", t, ["tenant_id", "line_hash"])
    enable_rls(t)
    enable_org_rls(t)

    t = "tds_recon_matches"
    op.create_table(
        t,
        *_base(t),
        sa.Column("statement_line_id", sa.Uuid(), nullable=False),
        sa.Column("tds_deduction_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(15), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("score", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_actor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("carried_from_id", sa.Uuid(), nullable=True),
        _fk(t, "statement_line_id", "tds_statement_lines", "fk_tds_recon_matches_statement_line_id"),
        _fk(t, "tds_deduction_id", "tds_deductions", "fk_tds_recon_matches_tds_deduction_id"),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'unmatched_ack')",
            name=op.f("ck_tds_recon_matches_status"),
        ),
        sa.CheckConstraint(
            "status = 'unmatched_ack' OR tds_deduction_id IS NOT NULL",
            name=op.f("ck_tds_recon_matches_target"),
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name=op.f("ck_tds_recon_matches_score")),
    )
    op.create_index("ix_tds_recon_matches_statement_line_id", t, ["statement_line_id"])
    op.create_index("ix_tds_recon_matches_tds_deduction_id", t, ["tds_deduction_id"])
    op.create_index(
        "uq_tds_recon_matches_open_line",
        t,
        ["statement_line_id"],
        unique=True,
        postgresql_where=sa.text("active AND status IN ('proposed', 'confirmed', 'unmatched_ack')"),
    )
    op.create_index(
        "uq_tds_recon_matches_confirmed_deduction",
        t,
        ["tds_deduction_id"],
        unique=True,
        postgresql_where=sa.text("active AND status = 'confirmed'"),
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in TABLES:
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
