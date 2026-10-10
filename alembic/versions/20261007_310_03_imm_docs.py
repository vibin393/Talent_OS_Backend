"""Epic 46 story 46.3 (FR-234, AD-28): ``case_document_requests`` -- checklist items that point at P2
``documents`` rows (owner ``immigration_case``, access level ``hr``, retention class ``immigration``).

Worker org RLS on ``candidate_id`` (the Worker sees and uploads their own requested items only).

Revision ID: 20261007_310_03_imm_docs
Revises: 20261007_310_02_imm_cases
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_310_03_imm_docs"
down_revision: str | tuple[str, ...] | None = "20261007_310_02_imm_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    t = "case_document_requests"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("checklist_key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("document_category", sa.String(40), nullable=False),
        sa.Column("dependant_id", sa.Uuid(), nullable=True),
        sa.Column("collect_from", sa.String(10), nullable=False),
        sa.Column("mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "collect_from IN ('worker', 'counsel', 'internal')",
            name=op.f("ck_case_document_requests_collect_from"),
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'uploaded', 'verified', 'rejected', 'waived')",
            name=op.f("ck_case_document_requests_status"),
        ),
        _fk("fk_case_document_requests_case_id", "case_id", "immigration_cases", "CASCADE"),
        _fk("fk_case_document_requests_candidate_id", "candidate_id", "candidates"),
        _fk("fk_case_document_requests_dependant_id", "dependant_id", "case_dependants", "CASCADE"),
    )
    op.create_index("ix_case_document_requests_tenant_id", t, ["tenant_id"])
    op.create_index("ix_case_document_requests_case_id", t, ["case_id"])
    op.create_index("ix_case_document_requests_candidate_id", t, ["candidate_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")


def downgrade() -> None:
    disable_org_rls("case_document_requests")
    disable_rls("case_document_requests")
    op.drop_table("case_document_requests")
