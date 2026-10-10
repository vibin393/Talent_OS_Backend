# ruff: noqa: E501
"""Epic 17 204_03: ``vendor_submissions`` (FR-95; the only candidate data the Vendor Portal reads, AD-26), their
vendor-visible timeline ``vendor_submission_events`` and the internal-only ``vendor_candidate_ownerships``
(FR-96: one active owner per client and candidate).

Revision ID: 20261004_204_03_vendor_subs
Revises: 20261004_204_02_vendor_releases
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_204_03_vendor_subs"
down_revision: str | tuple[str, ...] | None = "20261004_204_02_vendor_releases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def tfk(
    table: str, column: str, target: str, name: str | None = None, ondelete: str = "RESTRICT"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def base(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]
    if timestamps:
        cols += [
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
        ]
    return cols


def upgrade() -> None:
    t = "vendor_submissions"
    op.create_table(
        t,
        *base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("vendor_release_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_vendor_user_id", sa.Uuid(), nullable=True),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone_e164", sa.String(20), nullable=True),
        sa.Column("current_location", sa.String(200), nullable=True),
        sa.Column("total_experience_months", sa.Integer(), nullable=True),
        sa.Column("availability_date", sa.Date(), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("vendor_rate", sa.Numeric(14, 4), nullable=False),
        sa.Column("rate_unit", sa.String(10), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("resume_file_id", sa.Uuid(), nullable=True),
        sa.Column("resume_draft_id", sa.Uuid(), nullable=True),
        sa.Column("attestation_text", sa.Text(), nullable=False),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(15), server_default=sa.text("'submitted'"), nullable=False),
        sa.Column("duplicate_reason_category", sa.String(30), nullable=True),
        sa.Column("reject_reason_code", sa.String(40), nullable=True),
        sa.Column("reason_text_internal", sa.Text(), nullable=True),
        sa.Column("vendor_visible_reason", sa.String(100), nullable=True),
        sa.Column(
            "duplicate_findings", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("notice_consent", sa.String(10), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("progress_key", sa.String(40), nullable=True),
        sa.Column("progress_label", sa.String(60), nullable=True),
        sa.Column("progress_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_interview_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('submitted', 'under_review', 'accepted', 'rejected', 'duplicate', 'withdrawn')",
            name=op.f("ck_vendor_submissions_status"),
        ),
        sa.CheckConstraint(
            "duplicate_reason_category IS NULL OR duplicate_reason_category IN "
            "('owned_by_other_vendor', 'existing_candidate', 'submitted_by_other_vendor', 'other')",
            name=op.f("ck_vendor_submissions_duplicate_category"),
        ),
        sa.CheckConstraint("vendor_rate >= 0", name=op.f("ck_vendor_submissions_vendor_rate")),
        sa.CheckConstraint(
            "notice_consent IS NULL OR notice_consent IN ('granted', 'withdrawn')",
            name=op.f("ck_vendor_submissions_notice_consent"),
        ),
        tfk(
            t,
            "vendor_release_id",
            "vendor_releases",
            "fk_vendor_submissions_vendor_release_id_vendor_releases",
        ),
        tfk(t, "vendor_id", "vendors"),
        tfk(t, "demand_id", "demands"),
        tfk(t, "candidate_id", "candidates", ondelete="SET NULL"),
        tfk(t, "application_id", "applications", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_vendor_submissions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_vendor_submissions_tenant_id_human_id"),
    )
    op.create_index("ix_vendor_submissions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_vendor_submissions_vendor_id", t, ["vendor_id"])
    op.create_index("ix_vendor_submissions_release_id", t, ["vendor_release_id"])
    op.create_index("ix_vendor_submissions_demand_id", t, ["demand_id"])
    op.create_index("ix_vendor_submissions_application_id", t, ["application_id"])
    op.create_index("ix_vendor_submissions_email", t, ["tenant_id", "email"])

    e = "vendor_submission_events"
    op.create_table(
        e,
        *base(e, timestamps=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        tfk(e, "submission_id", "vendor_submissions", "fk_vendor_submission_events_submission_id", "CASCADE"),
    )
    op.create_index("ix_vendor_submission_events_tenant_id", e, ["tenant_id"])
    op.create_index("ix_vendor_submission_events_submission_id", e, ["submission_id"])

    o = "vendor_candidate_ownerships"
    op.create_table(
        o,
        *base(o),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_submission_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        tfk(o, "vendor_id", "vendors"),
        tfk(
            o,
            "candidate_id",
            "candidates",
            "fk_vendor_candidate_ownerships_candidate_id_candidates",
            "CASCADE",
        ),
        tfk(o, "vendor_submission_id", "vendor_submissions", "fk_vendor_candidate_ownerships_submission_id"),
    )
    op.create_index("ix_vendor_candidate_ownerships_tenant_id", o, ["tenant_id"])
    op.create_index("ix_vendor_candidate_ownerships_candidate_id", o, ["candidate_id"])
    op.create_index(
        "uq_vendor_candidate_ownerships_active",
        o,
        ["tenant_id", "client_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    for table in (t, e, o):
        enable_rls(table)
    enable_org_rls(t, vendor_col="vendor_id")
    enable_org_rls(e, vendor_col="vendor_id")
    enable_org_rls(o)  # internal-only: ownership never reaches a vendor session


def downgrade() -> None:
    for table in ("vendor_candidate_ownerships", "vendor_submission_events", "vendor_submissions"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
