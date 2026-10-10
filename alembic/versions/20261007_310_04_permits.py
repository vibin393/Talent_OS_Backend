# ruff: noqa: E501
"""Epic 46 story 46.4 (FR-235, AD-52): ``permits`` -- recorded on an Approved outcome (or an amendment / renewal of
an approved Case). Number ``EncryptedStr`` + ``number_last4`` + ``number_bidx`` (AD-35). Status is derived, never
stored. ``credential_id`` back-links the P2 Credential created by the compliance ``PermitRecorded`` handler.

Worker org RLS on ``candidate_id``.

Revision ID: 20261007_310_04_permits
Revises: 20261007_310_03_imm_docs
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_310_04_permits"
down_revision: str | tuple[str, ...] | None = "20261007_310_03_imm_docs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(name: str, col: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "permits"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("visa_type_id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.CHAR(2), nullable=False),
        sa.Column("permit_type", sa.String(200), nullable=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("number_last4", sa.String(4), nullable=True),
        sa.Column("number_bidx", sa.String(32), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("expiry_values", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "recorded_actor", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "number_last4 IS NULL OR length(number_last4) <= 4", name=op.f("ck_permits_last4")
        ),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name=op.f("ck_permits_dates")),
        _fk("fk_permits_case_id", "case_id", "immigration_cases"),
        _fk("fk_permits_candidate_id", "candidate_id", "candidates"),
        _fk("fk_permits_visa_type_id", "visa_type_id", "visa_types"),
    )
    op.create_index("ix_permits_tenant_id", t, ["tenant_id"])
    op.create_index("ix_permits_case_id", t, ["case_id"])
    op.create_index("ix_permits_candidate_id", t, ["candidate_id"])
    op.create_index("ix_permits_valid_to", t, ["valid_to"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")


def downgrade() -> None:
    disable_org_rls("permits")
    disable_rls("permits")
    op.drop_table("permits")
