# ruff: file-ignore[line-too-long]
"""Employee referrals (Epic 42, branch ``p3_referrals``, migrations ``20261006_306_01..03``; FR-213..216, AD-50).

* ``referral_settings`` -- one row per tenant (duplicate window, link lifetimes, default owner, payout currency).
* ``referral_programs`` -- eligibility, reward rules (Decimal money, AD-20), joining condition and exclusions.
  Configuration without personal data: tenant RLS only.
* ``referrals`` -- one referral of one referee by a User or a Worker (exactly one referrer column, matching
  ``referrer_kind``). Nothing about the referee is processed before the referee accepts the ``referral`` notice:
  until then only the referrer-entered contact data and the resume file exist. The link to the pipeline is
  ``referrals.application_id`` (UNIQUE) plus ``applications.source_ref`` (no ``applications.referral_id``).
  Worker org RLS on ``referrer_candidate_id`` (a Worker portal session sees only its own referrals).
* ``referral_share_links`` -- a personal multi-use share link per (referrer, demand). Worker org RLS.
* ``referral_rewards`` -- one reward per referral (UNIQUE), ``pending_condition -> eligible -> approved* -> paid``
  or ``void``. Worker org RLS on the denormalised ``referrer_candidate_id``.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
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
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

REFERRER_KINDS = ("user", "worker")
ELIGIBLE_REFERRERS = ("users", "workers", "both")
CHANNELS = ("upload", "share_link")
REFERRAL_STATUSES = (
    "submitted",
    "accepted",
    "in_process",
    "hired",
    "not_selected",
    "duplicate",
    "declined",
    "expired",
    "withdrawn",
)
CLOSED_STATUSES = ("declined", "expired", "withdrawn")
INELIGIBLE_REASONS = ("duplicate", "excluded_referrer", "no_program", "self_referral")
REWARD_STATUSES = ("pending_condition", "eligible", "approved", "paid", "void")
PAYOUT_CHANNELS = ("payroll", "payout_export")
VOID_REASONS = ("placement_dropped", "condition_not_met", "rejected", "manual")

REFERRER_CHECK = (
    "(referrer_kind = 'user' AND referrer_user_id IS NOT NULL AND referrer_candidate_id IS NULL) OR "
    "(referrer_kind = 'worker' AND referrer_candidate_id IS NOT NULL AND referrer_user_id IS NULL)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ settings / programs (306_01)
class ReferralSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "referral_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("duplicate_activity_months BETWEEN 1 AND 60", name="dup_months"),
        CheckConstraint("referee_link_days BETWEEN 1 AND 90", name="referee_days"),
        CheckConstraint("share_link_days BETWEEN 1 AND 365", name="share_days"),
        _tenant_fk("default_owner_user_id", "users", "fk_referral_settings_default_owner"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    duplicate_activity_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    referee_link_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("14"))
    share_link_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("90"))
    default_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payout_export_currency_default: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default=text("'INR'")
    )


class ReferralProgram(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "referral_programs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("eligible_referrers", ELIGIBLE_REFERRERS), name="eligible_referrers"),
        CheckConstraint("default_amount IS NULL OR default_amount > 0", name="default_amount"),
        CheckConstraint("joining_condition_days BETWEEN 0 AND 730", name="condition_days"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity"),
        Index("ix_referral_programs_active", "active", "priority"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    eligible_referrers: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'both'"))
    eligible_demands: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("""'{"all": true}'::jsonb""")
    )
    reward_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    default_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    joining_condition_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("90"))
    exclusions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ referrals / share links (306_02)
class ReferralShareLink(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The raw link token is kept sealed (tenant key) so the referrer can copy the same link again."""

    __tablename__ = "referral_share_links"
    __audit_exclude__ = ("token_sealed",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("referrer_kind", REFERRER_KINDS), name="referrer_kind"),
        CheckConstraint(REFERRER_CHECK, name="referrer"),
        _tenant_fk("referrer_user_id", "users", "fk_referral_share_links_user_id"),
        _tenant_fk("referrer_candidate_id", "candidates", "fk_referral_share_links_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_referral_share_links_demand_id"),
        Index(
            "uq_referral_share_links_active",
            "tenant_id",
            "referrer_kind",
            text("COALESCE(referrer_user_id, referrer_candidate_id)"),
            text("COALESCE(demand_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_referral_share_links_candidate", "referrer_candidate_id"),
    )

    referrer_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    referrer_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    referrer_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_sealed: Mapped[Sealed] = mapped_column(EncryptedStr(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Referral(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_referrals_human_id"),
        UniqueConstraint("application_id", name="uq_referrals_application_id"),
        CheckConstraint(_in("referrer_kind", REFERRER_KINDS), name="referrer_kind"),
        CheckConstraint(REFERRER_CHECK, name="referrer"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", REFERRAL_STATUSES), name="status"),
        CheckConstraint(
            f"ineligible_reason IS NULL OR {_in('ineligible_reason', INELIGIBLE_REASONS)}",
            name="ineligible_reason",
        ),
        _tenant_fk("program_id", "referral_programs", "fk_referrals_program_id"),
        _tenant_fk("demand_id", "demands", "fk_referrals_demand_id"),
        _tenant_fk("referrer_user_id", "users", "fk_referrals_referrer_user_id"),
        _tenant_fk("referrer_candidate_id", "candidates", "fk_referrals_referrer_candidate_id"),
        _tenant_fk("candidate_id", "candidates", "fk_referrals_candidate_id"),
        _tenant_fk("application_id", "applications", "fk_referrals_application_id"),
        _tenant_fk("placement_id", "placements", "fk_referrals_placement_id"),
        _tenant_fk("resume_file_id", "stored_files", "fk_referrals_resume_file_id"),
        _tenant_fk("share_link_id", "referral_share_links", "fk_referrals_share_link_id"),
        Index(
            "uq_referrals_open_referee",
            "tenant_id",
            "demand_id",
            text("lower(referee_email::text)"),
            unique=True,
            postgresql_where=text(
                "demand_id IS NOT NULL AND referee_email IS NOT NULL "
                "AND status NOT IN ('declined', 'expired', 'withdrawn')"
            ),
        ),
        Index("ix_referrals_status", "status"),
        Index("ix_referrals_demand_id", "demand_id"),
        Index("ix_referrals_referrer_user_id", "referrer_user_id"),
        Index("ix_referrals_referrer_candidate_id", "referrer_candidate_id"),
        Index("ix_referrals_candidate_id", "candidate_id"),
        Index("ix_referrals_placement_id", "placement_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    program_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    referrer_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    referrer_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    referrer_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    referee_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    referee_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    referee_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    relationship: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    share_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'submitted'"))
    reward_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ineligible_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ rewards (306_03)
class ReferralReward(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "referral_rewards"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("referral_id", name="uq_referral_rewards_referral_id"),
        CheckConstraint(_in("status", REWARD_STATUSES), name="status"),
        CheckConstraint(_in("payout_channel", PAYOUT_CHANNELS), name="payout_channel"),
        CheckConstraint(
            "payout_channel <> 'payroll' OR payee_placement_id IS NOT NULL", name="payroll_payee"
        ),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint(_in("referrer_kind", REFERRER_KINDS), name="referrer_kind"),
        CheckConstraint(REFERRER_CHECK, name="referrer"),
        CheckConstraint(f"void_reason IS NULL OR {_in('void_reason', VOID_REASONS)}", name="void_reason"),
        _tenant_fk("referral_id", "referrals", "fk_referral_rewards_referral_id"),
        _tenant_fk("program_id", "referral_programs", "fk_referral_rewards_program_id"),
        _tenant_fk("placement_id", "placements", "fk_referral_rewards_placement_id"),
        _tenant_fk("payee_placement_id", "placements", "fk_referral_rewards_payee_placement"),
        _tenant_fk("referrer_user_id", "users", "fk_referral_rewards_referrer_user_id"),
        _tenant_fk("referrer_candidate_id", "candidates", "fk_referral_rewards_referrer_cand"),
        Index("ix_referral_rewards_status", "status", "payout_channel"),
        Index("ix_referral_rewards_placement_id", "placement_id"),
        Index("ix_referral_rewards_referrer_cand", "referrer_candidate_id"),
    )

    referral_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    program_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    referrer_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    referrer_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    referrer_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending_condition'")
    )
    condition_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_channel: Mapped[str] = mapped_column(String(15), nullable=False)
    payee_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
