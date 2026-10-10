"""Offer tables (Epic 8: offers, immutable versions, letter templates, status history, candidate responses).

All tenant-owned with forced RLS. Tenant-internal references use composite foreign keys
``(tenant_id, x_id) -> x(tenant_id, id)`` because FK checks bypass RLS (same convention as Epic 6).

Migrations: 20261001_80 (settings, templates + seed, offers, versions, status history), 20261001_81
(responses, ``offers.expiry_warned_at`` / ``accepted_file_id``).

* One non-terminal offer per application: partial unique index on ``(tenant_id, application_id)``.
* ``offer_versions`` rows freeze when submitted for approval; approved / released versions never change.
* ``offer_responses`` keep the e-acceptance evidence (typed name, IP, user agent, letter SHA-256). Those values
  are encrypted in the audit log with the candidate's data key (``__pii_fields__``, AD-5 / FR-68).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

OFFER_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "released",
    "viewed",
    "accepted",
    "declined",
    "expired",
    "withdrawn",
)
TERMINAL_STATUSES = ("accepted", "declined", "expired", "withdrawn")
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")
WORK_MODES = ("onsite", "hybrid", "remote")
RATE_UNITS = ("hour", "day", "month", "year")
APPROVAL_STATES = ("not_required", "pending", "approved", "returned")
ACTOR_KINDS = ("user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app")  # +agent (400_01), +app (500_01)
DECISIONS = ("accepted", "declined")
CHANNELS = ("portal", "secure_link")

MONEY = Numeric(14, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ settings
class OfferSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily (``GET/PATCH /offers/settings``)."""

    __tablename__ = "offer_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("default_expiry_days BETWEEN 1 AND 90", name="default_expiry_days"),
        CheckConstraint("response_link_grace_hours BETWEEN 0 AND 168", name="grace_hours"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    default_expiry_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    signatory_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signatory_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    response_link_grace_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attach_letter_to_release_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Epic 22 (209_02): the candidate confirms an emailed code before accepting (built-in eSign).
    acceptance_requires_otp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


# ================================================================ templates
class OfferTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offer_templates"
    __audit_exclude__ = ("body_md",)
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    employment_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ offers
class Offer(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offers"
    __audit_exclude__ = ("withdrawn_reason", "status_comment")
    __table_args__ = (
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("application_id", "applications", "fk_offers_application_id_applications"),
        _tenant_fk("demand_id", "demands", "fk_offers_demand_id_demands"),
        _tenant_fk("candidate_id", "candidates", "fk_offers_candidate_id_candidates"),
        _tenant_fk("client_id", "clients", "fk_offers_client_id_clients"),
        _tenant_fk("owner_id", "users", "fk_offers_owner_id_users"),
        CheckConstraint(_in("status", OFFER_STATUSES), name="status"),
        Index(
            "uq_offers_open_per_application",
            "tenant_id",
            "application_id",
            unique=True,
            postgresql_where=text(f"NOT ({_in('status', TERMINAL_STATUSES)})"),
        ),
        Index("ix_offers_demand_id", "demand_id"),
        Index("ix_offers_candidate_id", "candidate_id"),
        Index("ix_offers_status_expires_at", "status", "expires_at"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    released_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 20261001_81: 24 h expiry warning claim, and the accepted letter with its certificate page.
    expiry_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 22 (209_02): the e-acceptance signature request of the released letter.
    signature_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 19 (206_03): the placement made from this offer was dropped -- no longer counts as filled.
    placement_dropped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class OfferVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offer_versions"
    __audit_exclude__ = ("rendered_body", "benefits_summary")
    __table_args__ = (
        UniqueConstraint("offer_id", "version_no"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_versions_offer_id_offers",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("employment_type", EMPLOYMENT_TYPES), name="employment_type"),
        CheckConstraint(_in("pay_rate_unit", RATE_UNITS), name="pay_rate_unit"),
        CheckConstraint(
            f"bill_rate_unit IS NULL OR {_in('bill_rate_unit', RATE_UNITS)}", name="bill_rate_unit"
        ),
        CheckConstraint(f"work_mode IS NULL OR {_in('work_mode', WORK_MODES)}", name="work_mode"),
        CheckConstraint(_in("approval_state", APPROVAL_STATES), name="approval_state"),
        CheckConstraint("pay_rate > 0", name="pay_rate_positive"),
        CheckConstraint("bill_rate IS NULL OR bill_rate > 0", name="bill_rate_positive"),
        CheckConstraint("bonus_amount IS NULL OR bonus_amount > 0", name="bonus_positive"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    employment_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pay_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    pay_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pay_rate_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    bill_rate: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    bill_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    bill_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    burden_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    margin_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    bonus_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    bonus_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    benefits_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rendered_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    letter_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    letter_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Hash of the render inputs (rendered Markdown + header/footer facts): makes the render task idempotent.
    letter_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class OfferStatusHistory(UUIDPk, TenantMixin, Base):
    """Append-only status log (written only by ``transition_offer``)."""

    __tablename__ = "offer_status_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_status_history_offer_id_offers",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("actor_kind", ACTOR_KINDS), name="actor_kind"),
        Index("ix_offer_status_history_offer_id_at", "offer_id", "at"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OfferResponse(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The candidate's answer. Evidence of the e-acceptance (FR-45); immutable once written."""

    __tablename__ = "offer_responses"
    __pii_fields__ = ("typed_name", "ip", "user_agent")
    __pii_subject__ = "candidate"
    __audit_entity_id__ = "candidate_id"
    __audit_exclude__ = ("decline_reason",)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_responses_offer_id_offers",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("decision", DECISIONS), name="decision"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        Index(
            "uq_offer_responses_accepted_offer",
            "offer_id",
            unique=True,
            postgresql_where=text("decision = 'accepted'"),
        ),
        Index("ix_offer_responses_offer_id", "offer_id"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    typed_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    letter_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
