"""Epic 35 222_04: ``ai_actions.meta`` (NFR-24 redaction counts, ``subject: none`` for subject-less runs).

Revision ID: 20261005_222_04_ai_meta
Revises: 20261005_222_03_notif_portal
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261005_222_04_ai_meta"
down_revision: str | tuple[str, ...] | None = "20261005_222_03_notif_portal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_actions", sa.Column("meta", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_actions", "meta")
