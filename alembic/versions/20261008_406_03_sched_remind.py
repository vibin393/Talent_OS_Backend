# ruff: noqa: E501
"""Epic 59 (Scheduling Agent, 59.5): the reminder / scorecard-nudge log.

``scheduling_reminders`` (internal) -- one row per interview participant, kind, offset and interview ``sequence``
(``uq_scheduling_reminders_natural``); a reschedule supersedes the previous sequence's rows.

Revision ID: 20261008_406_03_sched_remind
Revises: 20261008_406_02_sched_prefs
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_406_03_sched_remind"
down_revision: str | tuple[str, ...] | None = "20261008_406_02_sched_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = "scheduling_reminders"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'due'"), nullable=False),
        sa.Column("channel", sa.String(10), nullable=True),
        sa.Column("message_ref", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "interview_id",
            "participant_id",
            "kind",
            "offset_minutes",
            "sequence",
            name="uq_scheduling_reminders_natural",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_id"],
            ["interviews.tenant_id", "interviews.id"],
            name="fk_sched_reminders_interview_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "participant_id"],
            ["interview_participants.tenant_id", "interview_participants.id"],
            name="fk_sched_reminders_participant_id",
            ondelete="CASCADE",
        ),
        _ck(t, "kind", "kind IN ('before_interview', 'scorecard_nudge')"),
        _ck(t, "status", "status IN ('due', 'sent', 'skipped', 'failed', 'superseded')"),
    )
    op.create_index("ix_scheduling_reminders_status_due", t, ["status", "due_at"])
    op.create_index("ix_scheduling_reminders_candidate_id", t, ["candidate_id"])
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("scheduling_reminders")
    disable_rls("scheduling_reminders")
    op.drop_table("scheduling_reminders")
