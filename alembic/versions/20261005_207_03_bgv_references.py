# ruff: noqa: E501
"""Epic 20 207_03: reference checks, referees and referee responses.

Org RLS: ``reference_checks`` (``worker_col=candidate_id``); ``referees`` inherit ``candidate_id`` (the Worker
sees the referees it named); ``reference_responses`` are internal only (the restrictive policy has no worker
branch, so a Worker session never reads an answer).

Revision ID: 20261005_207_03_bgv_references
Revises: 20261005_207_02_bgv_cases_checks
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_207_03_bgv_references"
down_revision: str | tuple[str, ...] | None = "20261005_207_02_bgv_cases_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("reference_checks", "referees", "reference_responses")


def _base(table: str, *, timestamps: bool = True) -> list[object]:
    cols: list[object] = [
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


def _tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "reference_checks",
        *_base("reference_checks"),  # type: ignore[arg-type]
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("check_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("questionnaire_id", sa.Uuid(), nullable=True),
        sa.Column("questionnaire_snapshot", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("min_referees", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_reference_checks_tenant_id_id"),
        sa.CheckConstraint(
            "status IN ('requested', 'sent', 'responded', 'declined', 'expired', 'reviewed')", name="ck_reference_checks_status"
        ),
        sa.CheckConstraint("min_referees BETWEEN 1 AND 10", name="ck_reference_checks_min_referees"),
        _tfk("reference_checks", "candidate_id", "candidates"),
        _tfk("reference_checks", "case_id", "bgv_cases"),
        _tfk("reference_checks", "check_id", "bgv_checks"),
    )
    op.create_index("ix_reference_checks_tenant_id", "reference_checks", ["tenant_id"])
    op.create_index("ix_reference_checks_candidate_id", "reference_checks", ["candidate_id"])

    op.create_table(
        "referees",
        *_base("referees"),  # type: ignore[arg-type]
        sa.Column("reference_check_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("relationship", sa.String(100), nullable=True),
        sa.Column("company", sa.String(200), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_referees_tenant_id_id"),
        sa.CheckConstraint("status IN ('pending', 'sent', 'responded', 'declined', 'expired')", name="ck_referees_status"),
        _tfk("referees", "reference_check_id", "reference_checks", "CASCADE"),
    )
    op.create_index("ix_referees_tenant_id", "referees", ["tenant_id"])
    op.create_index("ix_referees_reference_check_id", "referees", ["reference_check_id"])

    op.create_table(
        "reference_responses",
        *_base("reference_responses", timestamps=False),  # type: ignore[arg-type]
        sa.Column("referee_id", sa.Uuid(), nullable=False),
        sa.Column("reference_check_id", sa.Uuid(), nullable=False),
        sa.Column("answers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint("referee_id", name="uq_reference_responses_referee_id"),
        _tfk("reference_responses", "referee_id", "referees", "CASCADE"),
        _tfk("reference_responses", "reference_check_id", "reference_checks", "CASCADE"),
    )
    op.create_index("ix_reference_responses_tenant_id", "reference_responses", ["tenant_id"])
    op.create_index("ix_reference_responses_reference_check_id", "reference_responses", ["reference_check_id"])

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("reference_checks", worker_col="candidate_id")
    enable_org_rls(
        "referees", worker_col="candidate_id", inherit_from=InheritRef("reference_checks", "reference_check_id", ("candidate_id",))
    )
    enable_org_rls("reference_responses")  # internal only: never the worker (FR-114)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
