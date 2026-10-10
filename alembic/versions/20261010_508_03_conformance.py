# ruff: noqa: E501
"""Epic 74 (Developer Portal, story 74.4, spec A2): platform table ``conformance_runs`` (observed App runs against a
sandbox; counters / flags and the signed ``talentos.conformance/1`` report only).

Revision ID: 20261010_508_03_conformance
Revises: 20261010_508_02_dev_sandboxes
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.settings import get_settings

revision: str = "20261010_508_03_conformance"
down_revision: str | tuple[str, ...] | None = "20261010_508_02_dev_sandboxes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("waiting", "running", "finishing", "passed", "failed", "expired", "cancelled")


def upgrade() -> None:
    role = get_settings().db_app_user
    t = "conformance_runs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("started_by_account_id", sa.Uuid(), nullable=True),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("window_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observations", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], name="fk_conformance_runs_app_id_apps", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sandbox_id"], ["developer_sandboxes.id"], name="fk_conformance_runs_sandbox_id_developer_sandboxes",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(s) for s in STATUSES) + ")", name=op.f("ck_conformance_runs_status")
        ),
    )
    op.create_index("ix_conformance_runs_app_id_created_at", t, ["app_id", sa.text("created_at DESC")])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {role}")


def downgrade() -> None:
    op.drop_table("conformance_runs")
