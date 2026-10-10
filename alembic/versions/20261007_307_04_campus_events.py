# ruff: noqa: E501
"""Epic 43 story 43.5 (FR-220): ``recruitment_events`` (the table name avoids the generic ``events``),
``event_checkins``, ``event_stations`` and ``queue_entries`` (server-side interview queue; ``position`` comes from
``recruitment_events.next_position`` under a row lock; index ``(event_id, status, position)`` for SKIP LOCKED calls).

Revision ID: 20261007_307_04_campus_events
Revises: 20261007_307_03_campus_batches
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_307_04_campus_events"
down_revision: str | tuple[str, ...] | None = "20261007_307_03_campus_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


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


def _link_fk(name: str, col: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([col], ["secure_links.id"], name=name, ondelete="SET NULL")


def _finish(table: str, *indexes: tuple[str, list[str]]) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    for name, cols in indexes:
        op.create_index(name, table, cols)
    enable_rls(table)
    enable_org_rls(table)


def upgrade() -> None:
    t = "recruitment_events"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("drive_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("demand_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        sa.Column("venue", sa.String(500), nullable=True),
        sa.Column("video_link", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("registration_link_id", sa.Uuid(), nullable=True),
        sa.Column("token_sealed", sa.Text(), nullable=True),
        sa.Column("walkin_link_id", sa.Uuid(), nullable=True),
        sa.Column("avg_interview_minutes", sa.Integer(), server_default=sa.text("20"), nullable=False),
        sa.Column("notify_channel", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("next_position", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            _in("kind", ("job_fair", "walk_in", "hackathon", "virtual", "campus")), name=op.f("ck_recruitment_events_kind")
        ),
        sa.CheckConstraint(
            _in("status", ("draft", "open", "live", "closed", "cancelled")), name=op.f("ck_recruitment_events_status")
        ),
        sa.CheckConstraint(
            _in("notify_channel", ("sms", "whatsapp", "none")), name=op.f("ck_recruitment_events_notify_channel")
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_recruitment_events_times")),
        sa.CheckConstraint("avg_interview_minutes BETWEEN 5 AND 180", name=op.f("ck_recruitment_events_avg_minutes")),
        _fk("fk_recruitment_events_drive_id", "drive_id", "campus_drives"),
        _fk("fk_recruitment_events_owner_id", "owner_id", "users"),
        _link_fk("fk_recruitment_events_reg_link", "registration_link_id"),
    )
    _finish(t, ("ix_recruitment_events_status", ["status"]))

    t = "event_checkins"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'registered'"), nullable=False),
        sa.Column("method", sa.String(12), server_default=sa.text("'registration'"), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkin_link_id", sa.Uuid(), nullable=True),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("event_id", "candidate_id", name="uq_event_checkins_event_candidate"),
        sa.CheckConstraint(
            _in("status", ("registered", "checked_in", "no_show", "left")), name=op.f("ck_event_checkins_status")
        ),
        sa.CheckConstraint(
            _in("method", ("qr", "manual", "walk_in", "registration")), name=op.f("ck_event_checkins_method")
        ),
        _fk("fk_event_checkins_event_id", "event_id", "recruitment_events"),
        _fk("fk_event_checkins_candidate_id", "candidate_id", "candidates"),
        _fk("fk_event_checkins_demand_id", "demand_id", "demands"),
        _link_fk("fk_event_checkins_link_id", "checkin_link_id"),
    )
    _finish(
        t,
        ("ix_event_checkins_candidate_id", ["candidate_id"]),
        ("ix_event_checkins_event_status", ["event_id", "status"]),
    )

    t = "event_stations"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("interviewer_user_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        *_ts(),
        sa.CheckConstraint(_in("status", ("open", "paused", "closed")), name=op.f("ck_event_stations_status")),
        _fk("fk_event_stations_event_id", "event_id", "recruitment_events", "CASCADE"),
        _fk("fk_event_stations_demand_id", "demand_id", "demands"),
    )
    _finish(t, ("ix_event_stations_event_id", ["event_id"]))

    t = "queue_entries"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("checkin_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_result", sa.String(40), nullable=True),
        *_ts(),
        sa.UniqueConstraint("checkin_id", name="uq_queue_entries_checkin_id"),
        sa.CheckConstraint(
            _in("status", ("waiting", "called", "in_interview", "done", "skipped", "left")),
            name=op.f("ck_queue_entries_status"),
        ),
        _fk("fk_queue_entries_event_id", "event_id", "recruitment_events"),
        _fk("fk_queue_entries_checkin_id", "checkin_id", "event_checkins", "CASCADE"),
        _fk("fk_queue_entries_candidate_id", "candidate_id", "candidates"),
        _fk("fk_queue_entries_demand_id", "demand_id", "demands"),
        _fk("fk_queue_entries_station_id", "station_id", "event_stations"),
        _fk("fk_queue_entries_application_id", "application_id", "applications"),
        _fk("fk_queue_entries_interview_id", "interview_id", "interviews"),
    )
    _finish(
        t,
        ("ix_queue_entries_event_status_pos", ["event_id", "status", "position"]),
        ("ix_queue_entries_candidate_id", ["candidate_id"]),
    )


def downgrade() -> None:
    for t in ("queue_entries", "event_stations", "event_checkins", "recruitment_events"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
