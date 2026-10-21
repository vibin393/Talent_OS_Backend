# ruff: noqa: E501
"""Unified RBAC: per-role module capability grants (``roles.module_caps``).

Additive JSONB column on the tenant-owned ``roles`` table. A role may grant coarse capabilities for the
other suite apps (Croar / Feedback IQ / Job Portal); these flow into the central SSO token's ``module_caps``
claim. Shape ``{app: [cap, ...]}``; default ``{}``. The tenant Admin grants every cap dynamically at resolve
time (``app.services.module_rbac.effective_role_module_caps``), so no backfill of existing admin roles is needed.

Phase 5 continuation on the governance line (unlabeled, like ``20261020_509_02_jn_stats``); chains onto the
Phase 5 merge so a plain ``alembic upgrade heads`` applies it after everything else.

Revision ID: 20261010_513_02_role_mcaps
Revises: 20261010_900_merge_phase5
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261010_513_02_role_mcaps"
down_revision: str | tuple[str, ...] | None = "20261010_900_merge_phase5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column(
            "module_caps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("roles", "module_caps")
