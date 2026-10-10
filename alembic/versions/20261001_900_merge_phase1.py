"""Phase 1 close-out: merge the parallel builder heads into a single Alembic head.

No schema change. After this revision ``alembic heads`` reports exactly one head.

Revision ID: 20261001_900
Revises: 20261001_112, 20261001_92
Create Date: 2026-10-01

"""

from collections.abc import Sequence

revision: str = "20261001_900"
down_revision: str | tuple[str, ...] | None = ("20261001_112", "20261001_92")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
