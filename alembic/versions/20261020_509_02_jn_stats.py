# ruff: noqa: E501
"""Epic 75 (Cross-tenant Job Network, story 75.5, spec A1 / A8): per-listing daily aggregate counts
``network_listing_stats`` (platform table; counts only -- no IP / UA / cookie / referrer / session id anywhere).

Revision ID: 20261020_509_02_jn_stats
Revises: 20261020_509_01_job_network
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.settings import get_settings

revision: str = "20261020_509_02_jn_stats"
down_revision: str | tuple[str, ...] | None = "20261020_509_01_job_network"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "network_listing_stats"


def upgrade() -> None:
    role = get_settings().db_app_user
    op.create_table(
        T,
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("views", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("apply_clicks", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("listing_id", "day", name=f"pk_{T}"),
        sa.ForeignKeyConstraint(
            ["listing_id"], ["network_listings.id"], name=f"fk_{T}_listing_id_network_listings", ondelete="CASCADE"
        ),
    )
    op.create_index(f"ix_{T}_day", T, ["day"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {T} TO {role}")


def downgrade() -> None:
    op.drop_table(T)
