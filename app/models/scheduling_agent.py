# ruff: noqa: E501
"""Scheduling Agent (Epic 59, FR-297..FR-300; migrations ``20261008_406_01_sched_offers`` / ``_406_02_sched_prefs`` /
``_406_03_sched_remind``). Owner: ``scheduling_agent``.

* ``scheduling_requests`` (audited) -- one "find a time" request of an Application for an interview-plan stage (``new``)
  or of a booked Interview (``reschedule``). One open request per (Application, stage) (``uq_sched_req_open``).
* ``scheduling_slot_offers`` -- one set of offered slots (= the foundation ``proposal_ref``); candidate-visible (worker
  org RLS on ``candidate_id``). One ``open`` offer per request; ``pick_id`` makes a pick idempotent.
* ``scheduling_request_events`` -- the history (ids and times only).
* ``interviewer_scheduling_prefs`` (audited) -- a user's time zone, working hours and holiday calendar.
* ``scheduling_agent_settings`` (audited) -- one row per tenant.
* ``scheduling_reminders`` -- reminder / scorecard-nudge rows per participant and interview ``sequence``.

``current_run_id`` / ``run_id`` are plain UUIDs (``agent_runs``; coordinator C-1). Free/busy only: no calendar event
content is ever stored.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

REQUEST_KINDS = ("new", "reschedule")
REQUEST_STATUSES = (
    "open", "slots_offered", "picked", "booked", "counter_proposed", "handed_off", "cancelled", "expired",
)  # fmt: skip
OPEN_REQUEST_STATUSES = ("open", "slots_offered", "picked", "counter_proposed")
HANDOFF_REASONS = (
    "no_common_slot",
    "max_rounds",
    "candidate_request",
    "calendar_missing",
    "model_failure",
    "other",
)
OFFER_STATUSES = ("draft", "open", "picked", "declined", "expired", "superseded")
EVENT_KINDS = (
    "requested", "slots_computed", "offered", "picked", "counter_proposed", "booked", "rescheduled", "conflict",
    "handed_off", "cancelled", "expired", "availability_requested",
)  # fmt: skip
REMINDER_KINDS = ("before_interview", "scorecard_nudge")
REMINDER_STATUSES = ("due", "sent", "skipped", "failed", "superseded")
MODES = ("video", "phone", "in_person")
OFFER_CHANNELS = ("email", "whatsapp", "sms", "portal")
DEFAULT_WORKING_HOURS: dict[str, list[list[str]]] = {
    d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")
}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ 406_01 requests, offers, history
class SchedulingRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "scheduling_requests"
    __audit_exclude__ = ("external_participants", "reschedule_reason")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_scheduling_requests_human_id"),
        _tenant_fk("application_id", "applications", "fk_sched_req_application_id"),
        _tenant_fk("candidate_id", "candidates", "fk_sched_req_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_sched_req_demand_id"),
        _tenant_fk("stage_id", "interview_stages", "fk_sched_req_stage_id"),
        _tenant_fk("interview_id", "interviews", "fk_sched_req_interview_id"),
        _tenant_fk("owner_user_id", "users", "fk_sched_req_owner_user_id"),
        CheckConstraint(_in("kind", REQUEST_KINDS), name="kind"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint("round BETWEEN 0 AND 10", name="round"),
        CheckConstraint("kind <> 'reschedule' OR interview_id IS NOT NULL", name="reschedule_interview"),
        CheckConstraint(
            f"handoff_reason IS NULL OR {_in('handoff_reason', HANDOFF_REASONS)}", name="handoff_reason"
        ),
        CheckConstraint("duration_minutes BETWEEN 15 AND 480", name="duration"),
        CheckConstraint("buffer_minutes BETWEEN 0 AND 120", name="buffer"),
        CheckConstraint(f"mode IS NULL OR {_in('mode', MODES)}", name="mode"),
        Index(
            "uq_sched_req_open",
            "tenant_id",
            "application_id",
            "stage_id",
            unique=True,
            postgresql_where=text("status IN ('open', 'slots_offered', 'picked', 'counter_proposed')"),
        ),
        Index("ix_scheduling_requests_application_id", "application_id"),
        Index("ix_scheduling_requests_candidate_id", "candidate_id"),
        Index("ix_scheduling_requests_interview_id", "interview_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'new'"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    round: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    duration_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    buffer_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    participant_user_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False)
    external_participants: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    candidate_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    booked_interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reschedule_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)


class SchedulingSlotOffer(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "scheduling_slot_offers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("request_id", "scheduling_requests", "fk_sched_offers_request_id", "CASCADE"),
        CheckConstraint(_in("status", OFFER_STATUSES), name="status"),
        CheckConstraint("picked_index IS NULL OR status = 'picked'", name="picked_index"),
        CheckConstraint(f"channel IS NULL OR {_in('channel', OFFER_CHANNELS)}", name="channel"),
        CheckConstraint("candidate_note IS NULL OR char_length(candidate_note) <= 500", name="note_length"),
        Index(
            "uq_sched_offers_open",
            "tenant_id",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "uq_sched_offers_pick_id",
            "tenant_id",
            "pick_id",
            unique=True,
            postgresql_where=text("pick_id IS NOT NULL"),
        ),
        Index("ix_scheduling_slot_offers_request_id", "request_id"),
        Index("ix_scheduling_slot_offers_candidate_id", "candidate_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    round: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    unknown_participants: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    channel: Mapped[str | None] = mapped_column(String(10), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    picked_index: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    picked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pick_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    counter_windows: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    candidate_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SchedulingRequestEvent(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "scheduling_request_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("request_id", "scheduling_requests", "fk_sched_events_request_id", "CASCADE"),
        CheckConstraint(_in("kind", EVENT_KINDS), name="kind"),
        Index("ix_scheduling_request_events_request_at", "request_id", "at"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ================================================================ 406_02 preferences, settings
class InterviewerSchedulingPrefs(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interviewer_scheduling_prefs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "user_id", name="uq_interviewer_scheduling_prefs_user"),
        _tenant_fk("user_id", "users", "fk_sched_prefs_user_id", "CASCADE"),
        CheckConstraint(
            "max_interviews_per_day IS NULL OR max_interviews_per_day BETWEEN 1 AND 20", name="max_per_day"
        ),
        CheckConstraint("buffer_minutes IS NULL OR buffer_minutes BETWEEN 0 AND 120", name="buffer"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    working_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    holiday_country: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    holiday_region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    max_interviews_per_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    buffer_minutes: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class SchedulingAgentSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "scheduling_agent_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_scheduling_agent_settings_tenant_id"),
        CheckConstraint("max_slots BETWEEN 1 AND 10", name="max_slots"),
        CheckConstraint("horizon_business_days BETWEEN 3 AND 20", name="horizon"),
        CheckConstraint("slot_step_minutes IN (15, 30, 60)", name="slot_step"),
        CheckConstraint("default_buffer_minutes BETWEEN 0 AND 60", name="default_buffer"),
        CheckConstraint("max_rounds BETWEEN 1 AND 5", name="max_rounds"),
        CheckConstraint("link_days BETWEEN 1 AND 14", name="link_days"),
        CheckConstraint("scorecard_nudge_after_hours BETWEEN 2 AND 72", name="nudge_after"),
        CheckConstraint("scorecard_nudge_max BETWEEN 0 AND 5", name="nudge_max"),
    )

    max_slots: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("5"))
    horizon_business_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10")
    )
    slot_step_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("30"))
    default_buffer_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("15")
    )
    candidate_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text('\'{"start": "08:00", "end": "21:00"}\'::jsonb')
    )
    max_rounds: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    link_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("7"))
    reminder_offsets_minutes: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{1440,60}'::integer[]")
    )
    reminder_channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default=text("'{email,whatsapp}'::varchar[]")
    )
    scorecard_nudge_after_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("24")
    )
    scorecard_nudge_max: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    default_working_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text(f"'{json.dumps(DEFAULT_WORKING_HOURS)}'::jsonb")
    )
    whatsapp_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ 406_03 reminders
class SchedulingReminder(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "scheduling_reminders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "interview_id",
            "participant_id",
            "kind",
            "offset_minutes",
            "sequence",
            name="uq_scheduling_reminders_natural",
        ),
        _tenant_fk("interview_id", "interviews", "fk_sched_reminders_interview_id", "CASCADE"),
        _tenant_fk(
            "participant_id", "interview_participants", "fk_sched_reminders_participant_id", "CASCADE"
        ),
        CheckConstraint(_in("kind", REMINDER_KINDS), name="kind"),
        CheckConstraint(_in("status", REMINDER_STATUSES), name="status"),
        Index("ix_scheduling_reminders_status_due", "status", "due_at"),
        Index("ix_scheduling_reminders_candidate_id", "candidate_id"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'due'"))
    channel: Mapped[str | None] = mapped_column(String(10), nullable=True)
    message_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "DEFAULT_WORKING_HOURS",
    "EVENT_KINDS",
    "HANDOFF_REASONS",
    "OFFER_STATUSES",
    "OPEN_REQUEST_STATUSES",
    "REQUEST_STATUSES",
    "InterviewerSchedulingPrefs",
    "SchedulingAgentSettings",
    "SchedulingReminder",
    "SchedulingRequest",
    "SchedulingRequestEvent",
    "SchedulingSlotOffer",
]
