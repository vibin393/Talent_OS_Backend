# ruff: noqa: E501
"""Epic 43 story 43.4 (FR-219): ``interview_batches`` and ``batch_slots`` (auto-slotted P1 interviews).

Revision ID: 20261007_307_03_campus_batches
Revises: 20261007_307_02_campus_regs
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_307_03_campus_batches"
down_revision: str | tuple[str, ...] | None = "20261007_307_02_campus_regs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "interview_batches"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("drive_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("panels", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("send_sms", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_ts(),
        sa.CheckConstraint(
            "status IN ('draft', 'scheduled', 'completed', 'cancelled')", name=op.f("ck_interview_batches_status")
        ),
        sa.CheckConstraint("slot_minutes BETWEEN 15 AND 180", name=op.f("ck_interview_batches_slot_minutes")),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_interview_batches_times")),
        _fk("fk_interview_batches_drive_id", "drive_id", "campus_drives"),
        _fk("fk_interview_batches_demand_id", "demand_id", "demands"),
    )
    op.create_index("ix_interview_batches_tenant_id", t, ["tenant_id"])
    op.create_index("ix_interview_batches_drive_id", t, ["drive_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "batch_slots"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("panel_index", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registration_id", sa.Uuid(), nullable=True),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("batch_id", "panel_index", "starts_at", name="uq_batch_slots_panel_start"),
        sa.CheckConstraint("panel_index >= 0", name=op.f("ck_batch_slots_panel_index")),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_batch_slots_times")),
        _fk("fk_batch_slots_batch_id", "batch_id", "interview_batches", "CASCADE"),
        _fk("fk_batch_slots_registration_id", "registration_id", "registrations"),
        _fk("fk_batch_slots_interview_id", "interview_id", "interviews"),
    )
    op.create_index("ix_batch_slots_tenant_id", t, ["tenant_id"])
    op.create_index("ix_batch_slots_registration_id", t, ["registration_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("batch_slots", "interview_batches"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
