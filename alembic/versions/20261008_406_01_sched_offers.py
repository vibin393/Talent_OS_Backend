# ruff: noqa: E501
"""Epic 59 (Scheduling Agent, stories 59.1-59.4): scheduling requests, slot offers and the request history.

* ``scheduling_requests`` (audited; internal) -- one open request per (Application, interview stage)
  (partial unique ``uq_sched_req_open``). ``current_run_id`` / ``booked_interview_id`` are plain UUIDs (C-1).
* ``scheduling_slot_offers`` (candidate-visible) -- the foundation ``proposal_ref``; one ``open`` offer per request,
  ``pick_id`` unique (a duplicate pick never double-books).
* ``scheduling_request_events`` (internal) -- the history (ids and times only).

Revision ID: 20261008_406_01_sched_offers
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_406_01_sched_offers"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_scheduling_agent",)
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "open",
    "slots_offered",
    "picked",
    "booked",
    "counter_proposed",
    "handed_off",
    "cancelled",
    "expired",
)
HANDOFF = ("no_common_slot", "max_rounds", "candidate_request", "calendar_missing", "model_failure", "other")
OFFER_STATUSES = ("draft", "open", "picked", "declined", "expired", "superseded")
EVENT_KINDS = (
    "requested", "slots_computed", "offered", "picked", "counter_proposed", "booked", "rescheduled", "conflict",
    "handed_off", "cancelled", "expired", "availability_requested",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "scheduling_requests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        _uuid("application_id", False),
        _uuid("candidate_id", False),
        _uuid("demand_id", False),
        _uuid("stage_id", False),
        _uuid("interview_id"),
        sa.Column("kind", sa.String(12), server_default=sa.text("'new'"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("round", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("buffer_minutes", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("participant_user_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column(
            "external_participants", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("mode", sa.String(10), nullable=True),
        sa.Column("candidate_timezone", sa.String(64), nullable=False),
        _uuid("owner_user_id", False),
        sa.Column("requested_by_kind", sa.String(20), nullable=False),
        _uuid("requested_by_id"),
        _uuid("current_run_id"),
        _uuid("booked_interview_id"),
        sa.Column("handoff_reason", sa.String(40), nullable=True),
        sa.Column("reschedule_reason", sa.String(300), nullable=True),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_scheduling_requests_human_id"),
        _fk("application_id", "applications", "fk_sched_req_application_id"),
        _fk("candidate_id", "candidates", "fk_sched_req_candidate_id"),
        _fk("demand_id", "demands", "fk_sched_req_demand_id"),
        _fk("stage_id", "interview_stages", "fk_sched_req_stage_id"),
        _fk("interview_id", "interviews", "fk_sched_req_interview_id"),
        _fk("owner_user_id", "users", "fk_sched_req_owner_user_id"),
        _ck(t, "kind", "kind IN ('new', 'reschedule')"),
        _ck(t, "status", _in("status", STATUSES)),
        _ck(t, "round", "round BETWEEN 0 AND 10"),
        _ck(t, "reschedule_interview", "kind <> 'reschedule' OR interview_id IS NOT NULL"),
        _ck(t, "handoff_reason", f"handoff_reason IS NULL OR {_in('handoff_reason', HANDOFF)}"),
        _ck(t, "duration", "duration_minutes BETWEEN 15 AND 480"),
        _ck(t, "buffer", "buffer_minutes BETWEEN 0 AND 120"),
        _ck(t, "mode", "mode IS NULL OR mode IN ('video', 'phone', 'in_person')"),
    )
    op.create_index(
        "uq_sched_req_open",
        t,
        ["tenant_id", "application_id", "stage_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'slots_offered', 'picked', 'counter_proposed')"),
    )
    op.create_index("ix_scheduling_requests_application_id", t, ["application_id"])
    op.create_index("ix_scheduling_requests_candidate_id", t, ["candidate_id"])
    op.create_index("ix_scheduling_requests_interview_id", t, ["interview_id"])

    t = "scheduling_slot_offers"
    op.create_table(
        t,
        *_base(t),
        _uuid("request_id", False),
        _uuid("candidate_id", False),
        sa.Column("round", sa.SmallInteger(), nullable=False),
        sa.Column("slots", postgresql.JSONB(), nullable=False),
        sa.Column(
            "unknown_participants",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("partial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("channel", sa.String(10), nullable=True),
        _uuid("secure_link_id"),
        _ts("sent_at"),
        _ts("expires_at"),
        sa.Column("picked_index", sa.SmallInteger(), nullable=True),
        _ts("picked_at"),
        _uuid("pick_id"),
        sa.Column("counter_windows", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_note", sa.String(500), nullable=True),
        _uuid("run_id"),
        _fk("request_id", "scheduling_requests", "fk_sched_offers_request_id", "CASCADE"),
        _ck(t, "status", _in("status", OFFER_STATUSES)),
        _ck(t, "picked_index", "picked_index IS NULL OR status = 'picked'"),
        _ck(t, "channel", "channel IS NULL OR channel IN ('email', 'whatsapp', 'sms', 'portal')"),
        _ck(t, "note_length", "candidate_note IS NULL OR char_length(candidate_note) <= 500"),
    )
    op.create_index(
        "uq_sched_offers_open",
        t,
        ["tenant_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "uq_sched_offers_pick_id",
        t,
        ["tenant_id", "pick_id"],
        unique=True,
        postgresql_where=sa.text("pick_id IS NOT NULL"),
    )
    op.create_index("ix_scheduling_slot_offers_request_id", t, ["request_id"])
    op.create_index("ix_scheduling_slot_offers_candidate_id", t, ["candidate_id"])

    t = "scheduling_request_events"
    op.create_table(
        t,
        *_base(t),
        _uuid("request_id", False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        _uuid("actor_id"),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _fk("request_id", "scheduling_requests", "fk_sched_events_request_id", "CASCADE"),
        _ck(t, "kind", _in("kind", EVENT_KINDS)),
    )
    op.create_index("ix_scheduling_request_events_request_at", t, ["request_id", "at"])

    for table in ("scheduling_requests", "scheduling_slot_offers", "scheduling_request_events"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
    enable_org_rls("scheduling_requests")
    enable_org_rls("scheduling_slot_offers", worker_col="candidate_id")
    enable_org_rls("scheduling_request_events")


def downgrade() -> None:
    for table in ("scheduling_request_events", "scheduling_slot_offers", "scheduling_requests"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
