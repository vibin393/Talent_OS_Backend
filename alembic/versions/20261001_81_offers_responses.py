"""Epic 8 offer responses (tenant-owned, RLS): the candidate's accept / decline with the e-acceptance evidence,
plus ``offers.expiry_warned_at`` (24 h warning claim) and ``offers.accepted_file_id`` (signed letter).

Revision ID: 20261001_81
Revises: 20261001_80
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_81"
down_revision: str | tuple[str, ...] | None = "20261001_80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("expiry_warned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("offers", sa.Column("accepted_file_id", sa.Uuid(), nullable=True))
    op.create_table(
        "offer_responses",
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("typed_name", sa.String(length=300), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("letter_sha256", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("decision IN ('accepted', 'declined')", name=op.f("ck_offer_responses_decision")),
        sa.CheckConstraint("channel IN ('portal', 'secure_link')", name=op.f("ck_offer_responses_channel")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_responses_offer_id_offers",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_offer_responses_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_responses")),
    )
    op.create_index(op.f("ix_offer_responses_tenant_id"), "offer_responses", ["tenant_id"], unique=False)
    op.create_index(
        op.f("ix_offer_responses_candidate_id"), "offer_responses", ["candidate_id"], unique=False
    )
    op.create_index("ix_offer_responses_offer_id", "offer_responses", ["offer_id"], unique=False)
    op.create_index(
        "uq_offer_responses_accepted_offer",
        "offer_responses",
        ["offer_id"],
        unique=True,
        postgresql_where=sa.text("decision = 'accepted'"),
    )
    enable_rls("offer_responses")


def downgrade() -> None:
    disable_rls("offer_responses")
    op.drop_table("offer_responses")
    op.drop_column("offers", "accepted_file_id")
    op.drop_column("offers", "expiry_warned_at")
