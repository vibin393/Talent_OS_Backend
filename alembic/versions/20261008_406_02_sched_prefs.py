# ruff: noqa: E501
"""Epic 59 (Scheduling Agent, 59.1 / 59.5): interviewer scheduling preferences and the tenant settings row.

* ``interviewer_scheduling_prefs`` (audited; internal) -- one row per user: time zone, working hours, holiday calendar
  (country / region), max interviews per day, buffer.
* ``scheduling_agent_settings`` (audited; internal) -- one row per tenant (``uq_scheduling_agent_settings_tenant_id``).

Revision ID: 20261008_406_02_sched_prefs
Revises: 20261008_406_01_sched_offers
Create Date: 2026-10-08
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_406_02_sched_prefs"
down_revision: str | tuple[str, ...] | None = "20261008_406_01_sched_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_HOURS = json.dumps({d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")})


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _small(name: str, default: int) -> sa.Column:
    return sa.Column(name, sa.SmallInteger(), server_default=sa.text(str(default)), nullable=False)


def upgrade() -> None:
    t = "interviewer_scheduling_prefs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("working_hours", postgresql.JSONB(), nullable=False),
        sa.Column("holiday_country", sa.CHAR(2), nullable=True),
        sa.Column("holiday_region", sa.String(10), nullable=True),
        sa.Column("max_interviews_per_day", sa.SmallInteger(), nullable=True),
        sa.Column("buffer_minutes", sa.SmallInteger(), nullable=True),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_interviewer_scheduling_prefs_user"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_sched_prefs_user_id",
            ondelete="CASCADE",
        ),
        _ck(t, "max_per_day", "max_interviews_per_day IS NULL OR max_interviews_per_day BETWEEN 1 AND 20"),
        _ck(t, "buffer", "buffer_minutes IS NULL OR buffer_minutes BETWEEN 0 AND 120"),
    )

    t = "scheduling_agent_settings"
    op.create_table(
        t,
        *_base(t),
        _small("max_slots", 5),
        _small("horizon_business_days", 10),
        _small("slot_step_minutes", 30),
        _small("default_buffer_minutes", 15),
        sa.Column(
            "candidate_hours", postgresql.JSONB(), server_default=sa.text("'{\"start\": \"08:00\", \"end\": \"21:00\"}'::jsonb"),
            nullable=False,
        ),
        _small("max_rounds", 3),
        _small("link_days", 7),
        sa.Column(
            "reminder_offsets_minutes", postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{1440,60}'::integer[]"),
            nullable=False,
        ),
        sa.Column(
            "reminder_channels", postgresql.ARRAY(sa.String(10)), server_default=sa.text("'{email,whatsapp}'::varchar[]"),
            nullable=False,
        ),
        _small("scorecard_nudge_after_hours", 24),
        _small("scorecard_nudge_max", 2),
        sa.Column(
            "default_working_hours", postgresql.JSONB(), server_default=sa.text(f"'{DEFAULT_HOURS}'::jsonb"), nullable=False
        ),
        sa.Column("whatsapp_template_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_scheduling_agent_settings_tenant_id"),
        _ck(t, "max_slots", "max_slots BETWEEN 1 AND 10"),
        _ck(t, "horizon", "horizon_business_days BETWEEN 3 AND 20"),
        _ck(t, "slot_step", "slot_step_minutes IN (15, 30, 60)"),
        _ck(t, "default_buffer", "default_buffer_minutes BETWEEN 0 AND 60"),
        _ck(t, "max_rounds", "max_rounds BETWEEN 1 AND 5"),
        _ck(t, "link_days", "link_days BETWEEN 1 AND 14"),
        _ck(t, "nudge_after", "scorecard_nudge_after_hours BETWEEN 2 AND 72"),
        _ck(t, "nudge_max", "scorecard_nudge_max BETWEEN 0 AND 5"),
    )  # fmt: skip

    for table in ("interviewer_scheduling_prefs", "scheduling_agent_settings"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in ("scheduling_agent_settings", "interviewer_scheduling_prefs"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
