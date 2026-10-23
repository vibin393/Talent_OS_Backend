# ruff: file-ignore[line-too-long]
"""Campus hiring & recruitment events (Epic 43, branch ``p3_campus``, migrations ``20261007_307_01..04``;
FR-217..FR-221).

* 307_01 ``institutions`` / ``institution_contacts`` (placement officers -- coordinator decision 13: P1 Contacts need
  a Client, so TPOs live here with the same shape and rules: no login, PII fields, erasure), ``campus_settings``,
  ``campus_drives``, ``drive_demands``, ``drive_institutions`` (one registration Secure Link + QR per pair).
* 307_02 ``registrations`` (one per drive and Candidate; standard answers denormalised into typed columns for the
  ``campus.registration`` semantic source) and ``registration_invites`` (placement-officer imports: an invitation
  only -- no Candidate or consent until the student submits).
* 307_03 ``interview_batches`` / ``batch_slots`` (auto-slotted P1 interviews).
* 307_04 ``recruitment_events``, ``event_checkins``, ``event_stations``, ``queue_entries`` (server-side queue).

All tables are internal only: tenant RLS plus ``enable_org_rls`` without org columns (portal sessions see nothing).
The link to the pipeline is ``registrations.application_id`` / ``queue_entries.application_id`` plus
``applications.source_ref`` (no column on ``applications``).
"""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

INSTITUTION_TIERS = ("tier_1", "tier_2", "tier_3", "other")
INSTITUTION_STATUSES = ("active", "archived")
DRIVE_STATUSES = ("draft", "registration_open", "in_progress", "closed", "cancelled")
DRIVE_INSTITUTION_STATUSES = ("active", "paused")
REGISTRATION_STATUSES = (
    "registered",
    "eligible",
    "not_eligible",
    "assessed",
    "shortlisted",
    "interviewed",
    "offered",
    "joined",
    "reneged",
    "rejected",
    "withdrawn",
)
REGISTRATION_TERMINAL = ("joined", "reneged", "rejected", "withdrawn")
REGISTRATION_SOURCES = ("link", "import", "walk_in")
INVITE_STATUSES = ("pending", "sent", "registered", "expired")
BATCH_STATUSES = ("draft", "scheduled", "completed", "cancelled")
EVENT_KINDS = ("job_fair", "walk_in", "hackathon", "virtual", "campus")
EVENT_STATUSES = ("draft", "open", "live", "closed", "cancelled")
NOTIFY_CHANNELS = ("sms", "whatsapp", "none")
CHECKIN_STATUSES = ("registered", "checked_in", "no_show", "left")
CHECKIN_METHODS = ("qr", "manual", "walk_in", "registration")
STATION_STATUSES = ("open", "paused", "closed")
QUEUE_STATUSES = ("waiting", "called", "in_interview", "done", "skipped", "left")
QUEUE_ACTIVE = ("waiting", "called", "in_interview")

E164 = "^\\+[1-9][0-9]{6,14}$"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ 307_01 institutions / drives
class CampusSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults). Tenant Settings entry ``campus`` (audited)."""

    __tablename__ = "campus_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("register_ip_limit BETWEEN 1 AND 100", name="ip_limit"),
        CheckConstraint("register_drive_limit BETWEEN 100 AND 20000", name="drive_limit"),
        CheckConstraint("avg_interview_minutes BETWEEN 5 AND 180", name="avg_minutes"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    register_ip_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    register_drive_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3000"))
    avg_interview_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    poster_footer: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Institution(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "institutions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name", name="uq_institutions_tenant_id_name"),
        CheckConstraint(_in("tier", INSTITUTION_TIERS), name="tier"),
        CheckConstraint(_in("status", INSTITUTION_STATUSES), name="status"),
    )

    name: Mapped[str] = mapped_column(CITEXT, nullable=False)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, server_default=text("'IN'"))
    tier: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'other'"))
    programs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class InstitutionContact(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A placement officer (TPO): same shape and rules as a P1 Contact; no login."""

    __tablename__ = "institution_contacts"
    __pii_fields__ = ("name", "email", "phone")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(f"phone IS NULL OR phone ~ '{E164}'", name="phone_e164"),
        _tenant_fk("institution_id", "institutions", "fk_institution_contacts_institution"),
        Index(
            "uq_institution_contacts_email",
            "institution_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("ix_institution_contacts_institution_id", "institution_id"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CampusDrive(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "campus_drives"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", DRIVE_STATUSES), name="status"),
        CheckConstraint("batch_year BETWEEN 2000 AND 2100", name="batch_year"),
        CheckConstraint(
            "registration_closes_at IS NULL OR registration_opens_at IS NULL "
            "OR registration_closes_at > registration_opens_at",
            name="registration_window",
        ),
        _tenant_fk("owner_id", "users", "fk_campus_drives_owner_id_users"),
        _tenant_fk("assessment_id", "assessments", "fk_campus_drives_assessment_id"),
        _tenant_fk("interview_stage_template_id", "interview_stage_templates", "fk_campus_drives_stage_template"),
        _tenant_fk("offer_template_id", "offer_templates", "fk_campus_drives_offer_template"),
        Index("ix_campus_drives_status", "status"),
        Index("ix_campus_drives_owner_id", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    batch_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    registration_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registration_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assessment_window: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    interview_window: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    form_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("""'{"fields": []}'::jsonb""")
    )
    eligibility_definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    eligibility_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_stage_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    offer_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DriveDemand(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "drive_demands"
    __audit_entity_id__ = "drive_id"
    __table_args__ = (
        _tenant_fk("drive_id", "campus_drives", "fk_drive_demands_drive_id", "CASCADE"),
        _tenant_fk("demand_id", "demands", "fk_drive_demands_demand_id"),
        Index("ix_drive_demands_demand_id", "demand_id"),
    )

    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)


class DriveInstitution(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """One registration link + QR per drive and Institution. The raw link token is kept sealed (tenant key) so the
    recruiter can copy the same URL and re-download the poster while the link is active."""

    __tablename__ = "drive_institutions"
    __audit_exclude__ = ("token_sealed",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("drive_id", "institution_id", name="uq_drive_institutions_pair"),
        CheckConstraint(_in("status", DRIVE_INSTITUTION_STATUSES), name="status"),
        CheckConstraint("registration_cap IS NULL OR registration_cap >= 1", name="cap"),
        _tenant_fk("drive_id", "campus_drives", "fk_drive_institutions_drive_id", "CASCADE"),
        _tenant_fk("institution_id", "institutions", "fk_drive_institutions_institution_id"),
        _tenant_fk("qr_file_id", "stored_files", "fk_drive_institutions_qr_file_id"),
        ForeignKeyConstraint(
            ["registration_link_id"], ["secure_links.id"], name="fk_drive_institutions_link_id", ondelete="SET NULL"
        ),
        Index("ix_drive_institutions_institution_id", "institution_id"),
    )

    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    institution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    registration_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    token_sealed: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    qr_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    registration_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))


# ================================================================ 307_02 registrations
class Registration(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "registrations"
    __audit_entity__ = "campus_registration"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("drive_id", "candidate_id", name="uq_registrations_drive_candidate"),
        CheckConstraint(_in("status", REGISTRATION_STATUSES), name="status"),
        CheckConstraint(_in("source", REGISTRATION_SOURCES), name="source"),
        CheckConstraint("cgpa IS NULL OR cgpa BETWEEN 0 AND 10", name="cgpa"),
        CheckConstraint("percentage IS NULL OR percentage BETWEEN 0 AND 100", name="percentage"),
        CheckConstraint("backlogs IS NULL OR backlogs >= 0", name="backlogs"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="score"),
        _tenant_fk("drive_id", "campus_drives", "fk_registrations_drive_id"),
        _tenant_fk("drive_institution_id", "drive_institutions", "fk_registrations_drive_institution"),
        _tenant_fk("institution_id", "institutions", "fk_registrations_institution_id"),
        _tenant_fk("candidate_id", "candidates", "fk_registrations_candidate_id"),
        _tenant_fk("override_by", "users", "fk_registrations_override_by"),
        _tenant_fk("shortlisted_by", "users", "fk_registrations_shortlisted_by"),
        _tenant_fk("assessment_invitation_id", "assessment_invitations", "fk_registrations_invitation"),
        _tenant_fk("application_id", "applications", "fk_registrations_application_id"),
        _tenant_fk("offer_id", "offers", "fk_registrations_offer_id"),
        Index("ix_registrations_drive_status", "drive_id", "status"),
        Index("ix_registrations_drive_institution", "drive_id", "drive_institution_id"),
        Index("ix_registrations_candidate_id", "candidate_id"),
        Index("ix_registrations_invitation_id", "assessment_invitation_id"),
        Index("ix_registrations_application_id", "application_id"),
        Index("ix_registrations_offer_id", "offer_id"),
    )

    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    drive_institution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    institution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'registered'"))
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    program: Mapped[str | None] = mapped_column(String(200), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cgpa: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    backlogs: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    graduation_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    batch_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    eligibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    override_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    assessment_invitation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assessment_flag: Mapped[str | None] = mapped_column(String(20), nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    shortlisted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    planned_joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'link'"))
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shortlisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reneged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RegistrationInvite(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A placement-officer import row: a personal single-use registration link was emailed. No Candidate and no
    consent exist until the student submits; the email is nulled when the invite expires (``campus.tick``)."""

    __tablename__ = "registration_invites"
    __pii_fields__ = ("email", "name")
    __candidate_pii__ = True
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", INVITE_STATUSES), name="status"),
        _tenant_fk("drive_id", "campus_drives", "fk_registration_invites_drive_id"),
        _tenant_fk("drive_institution_id", "drive_institutions", "fk_registration_invites_di"),
        _tenant_fk("registration_id", "registrations", "fk_registration_invites_registration"),
        ForeignKeyConstraint(
            ["secure_link_id"], ["secure_links.id"], name="fk_registration_invites_link_id", ondelete="SET NULL"
        ),
        Index("ix_registration_invites_drive_id", "drive_id"),
    )

    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    drive_institution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prefill: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ================================================================ 307_03 interview batches
class InterviewBatch(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", BATCH_STATUSES), name="status"),
        CheckConstraint("slot_minutes BETWEEN 15 AND 180", name="slot_minutes"),
        CheckConstraint("end_time > start_time", name="times"),
        _tenant_fk("drive_id", "campus_drives", "fk_interview_batches_drive_id"),
        _tenant_fk("demand_id", "demands", "fk_interview_batches_demand_id"),
        Index("ix_interview_batches_drive_id", "drive_id"),
    )

    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    day: Mapped[date] = mapped_column("date", Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    panels: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    send_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class BatchSlot(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "batch_slots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("batch_id", "panel_index", "starts_at", name="uq_batch_slots_panel_start"),
        CheckConstraint("panel_index >= 0", name="panel_index"),
        CheckConstraint("ends_at > starts_at", name="times"),
        _tenant_fk("batch_id", "interview_batches", "fk_batch_slots_batch_id", "CASCADE"),
        _tenant_fk("registration_id", "registrations", "fk_batch_slots_registration_id"),
        _tenant_fk("interview_id", "interviews", "fk_batch_slots_interview_id"),
        Index("ix_batch_slots_registration_id", "registration_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    panel_index: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ 307_04 events / queue
class RecruitmentEvent(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "recruitment_events"
    __audit_exclude__ = ("token_sealed", "next_position")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", EVENT_KINDS), name="kind"),
        CheckConstraint(_in("status", EVENT_STATUSES), name="status"),
        CheckConstraint(_in("notify_channel", NOTIFY_CHANNELS), name="notify_channel"),
        CheckConstraint("ends_at > starts_at", name="times"),
        CheckConstraint("avg_interview_minutes BETWEEN 5 AND 180", name="avg_minutes"),
        _tenant_fk("drive_id", "campus_drives", "fk_recruitment_events_drive_id"),
        _tenant_fk("owner_id", "users", "fk_recruitment_events_owner_id"),
        ForeignKeyConstraint(
            ["registration_link_id"], ["secure_links.id"], name="fk_recruitment_events_reg_link", ondelete="SET NULL"
        ),
        Index("ix_recruitment_events_status", "status"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    drive_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))
    venue: Mapped[str | None] = mapped_column(String(500), nullable=True)
    video_link: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    registration_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    token_sealed: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    walkin_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    avg_interview_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    notify_channel: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    next_position: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EventCheckin(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "event_checkins"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("event_id", "candidate_id", name="uq_event_checkins_event_candidate"),
        CheckConstraint(_in("status", CHECKIN_STATUSES), name="status"),
        CheckConstraint(_in("method", CHECKIN_METHODS), name="method"),
        _tenant_fk("event_id", "recruitment_events", "fk_event_checkins_event_id"),
        _tenant_fk("candidate_id", "candidates", "fk_event_checkins_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_event_checkins_demand_id"),
        ForeignKeyConstraint(
            ["checkin_link_id"], ["secure_links.id"], name="fk_event_checkins_link_id", ondelete="SET NULL"
        ),
        Index("ix_event_checkins_candidate_id", "candidate_id"),
        Index("ix_event_checkins_event_status", "event_id", "status"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'registered'"))
    method: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'registration'"))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkin_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class EventStation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "event_stations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", STATION_STATUSES), name="status"),
        _tenant_fk("event_id", "recruitment_events", "fk_event_stations_event_id", "CASCADE"),
        _tenant_fk("demand_id", "demands", "fk_event_stations_demand_id"),
        Index("ix_event_stations_event_id", "event_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    interviewer_user_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))


class QueueEntry(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "queue_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("checkin_id", name="uq_queue_entries_checkin_id"),
        CheckConstraint(_in("status", QUEUE_STATUSES), name="status"),
        _tenant_fk("event_id", "recruitment_events", "fk_queue_entries_event_id"),
        _tenant_fk("checkin_id", "event_checkins", "fk_queue_entries_checkin_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_queue_entries_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_queue_entries_demand_id"),
        _tenant_fk("station_id", "event_stations", "fk_queue_entries_station_id"),
        _tenant_fk("application_id", "applications", "fk_queue_entries_application_id"),
        _tenant_fk("interview_id", "interviews", "fk_queue_entries_interview_id"),
        Index("ix_queue_entries_event_status_pos", "event_id", "status", "position"),
        Index("ix_queue_entries_candidate_id", "candidate_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    checkin_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'waiting'"))
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    station_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_result: Mapped[str | None] = mapped_column(String(40), nullable=True)
