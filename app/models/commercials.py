"""Client commercials (Epic 16, FR-88..FR-92) on top of the Epic 13 contract-layer hub tables (AD-36).

Hub tables ``contracts`` / ``purchase_orders`` come from migration 20261002_200_07 (keys, org columns, status,
Legal Entity); Epic 16 ALTERs them with the business columns (band 203, branch ``p2_commercials``) and adds
payment terms, billing profiles, contract documents / milestones / reminders, rate cards + lines, rate checks,
the append-only PO consumption ledger (AD-32) and PO alerts.
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CONTRACT_TYPES = ("msa", "sow", "amendment", "vendor_agreement")
# Epic 36 (300_03): SOW pricing (Epic 45); NULL on every non-SOW contract and on legacy SOWs.
PRICING_MODELS = ("time_and_materials", "fixed_price", "milestone")
CONTRACT_STATUSES = ("draft", "pending_approval", "active", "expired", "terminated")
# Foundation statuses; Epic 16 writes ``active`` (= open), ``closed`` and ``cancelled`` only -- validity
# (upcoming / valid / expired) is derived from the dates, never stored.
PO_STATUSES = ("active", "expired", "closed", "cancelled")
TAX_TREATMENTS = ("registered", "unregistered", "sez", "export")
DELIVERY_PREFERENCES = ("email", "portal", "email_and_portal")
PREFERRED_VENDOR_STATUSES = ("none", "empanelled", "preferred")
CONTACT_ROLES = ("to", "cc")
COUNTERPARTY_KINDS = ("client", "vendor")
CONVERSION_FEE_KINDS = ("none", "pct_of_ctc", "fixed")
CONTRACT_DOCUMENT_KINDS = ("draft", "signed", "annex", "other")
MILESTONE_STATUSES = ("planned", "accepted", "cancelled")
RATE_CARD_STATUSES = ("draft", "published", "superseded", "archived")
RATE_UNITS = ("hour", "day", "month", "year", "fixed")
RATE_CHECK_SUBJECT_KINDS = (
    "demand",
    "vendor_release",
    "submission",
    "offer",
    "placement",
    "extension",
    "rate_segment",
)
RATE_CHECK_RESULTS = ("ok", "out_of_range", "no_matching_line", "vendor_rate_exceeded", "not_validated")
EXCEPTION_STATUSES = ("none", "pending", "approved", "rejected")
CONSUMPTION_DOC_KINDS = ("invoice", "credit_note", "debit_note")
PO_ALERT_KINDS = ("threshold", "expiring", "validity")

MONEY = Numeric(16, 2)
RATE = Numeric(14, 4)
HOURS = Numeric(10, 2)
PCT = Numeric(7, 4)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ payment terms / settings
class PaymentTerm(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "payment_terms"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("days >= 0 AND days <= 365", name="days"),
        Index(
            "uq_payment_terms_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommercialsSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "commercials_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    contract_renewal_lead_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{60,30}'")
    )
    po_alert_thresholds_pct: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{80,100}'")
    )
    po_expiry_alert_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    rate_exception_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False)
    po_exception_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False)


# ================================================================ billing profile (FR-88)
class BillingProfile(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "client_id"),
        CheckConstraint(_in("tax_treatment", TAX_TREATMENTS), name="tax_treatment"),
        CheckConstraint(_in("delivery_preference", DELIVERY_PREFERENCES), name="delivery_preference"),
        CheckConstraint(
            _in("preferred_vendor_status", PREFERRED_VENDOR_STATUSES), name="preferred_vendor_status"
        ),
        CheckConstraint(
            "tds_rate_pct IS NULL OR (tds_rate_pct >= 0 AND tds_rate_pct <= 100)", name="tds_rate"
        ),
        _tenant_fk("client_id", "clients", "fk_billing_profiles_client_id_clients"),
        _tenant_fk("payment_terms_id", "payment_terms", "fk_billing_profiles_payment_terms_id_payment_terms"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    pan: Mapped[str | None] = mapped_column(CHAR(10), nullable=True)
    tax_treatment: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'registered'")
    )
    payment_terms_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    po_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    delivery_preference: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'email'")
    )
    tds_section_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tds_rate_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    gst_tds_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    preferred_vendor_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'none'")
    )
    msp_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vms_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vms_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)


class BillingAddress(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_addresses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("client_id", "clients", "fk_billing_addresses_client_id_clients"),
        _tenant_fk(
            "billing_profile_id",
            "billing_profiles",
            "fk_billing_addresses_billing_profile_id_billing_profiles",
            "CASCADE",
        ),
        ForeignKeyConstraint(
            ["state_code"], ["gst_states.code"], name="fk_billing_addresses_state_code_gst_states"
        ),
        Index(
            "uq_billing_addresses_default",
            "billing_profile_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
        Index("ix_billing_addresses_client_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    billing_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    line1: Mapped[str] = mapped_column(String(300), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state_code: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False, server_default=text("'IN'"))
    gstin: Mapped[str | None] = mapped_column(CHAR(15), nullable=True)
    is_sez: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingProfileContact(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "billing_profile_contacts"
    __table_args__ = (
        UniqueConstraint("billing_profile_id", "contact_id"),
        CheckConstraint(_in("role", CONTACT_ROLES), name="role"),
        _tenant_fk("client_id", "clients", "fk_billing_profile_contacts_client_id_clients"),
        _tenant_fk(
            "billing_profile_id",
            "billing_profiles",
            "fk_billing_profile_contacts_billing_profile_id_billing_profiles",
            "CASCADE",
        ),
        _tenant_fk("contact_id", "contacts", "fk_billing_profile_contacts_contact_id_contacts", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    billing_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(4), nullable=False)


# ================================================================ contracts (FR-89)
class Contract(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("contract_type", CONTRACT_TYPES), name="contract_type"),
        CheckConstraint(_in("status", CONTRACT_STATUSES), name="status"),
        CheckConstraint("(client_id IS NULL) <> (vendor_id IS NULL)", name="one_party"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
        CheckConstraint(_in("counterparty_kind", COUNTERPARTY_KINDS), name="counterparty_kind"),
        CheckConstraint(
            "(counterparty_kind = 'vendor') = (vendor_id IS NOT NULL)", name="counterparty_matches"
        ),
        CheckConstraint(
            "(contract_type = 'vendor_agreement') = (vendor_id IS NOT NULL)", name="vendor_agreement"
        ),
        CheckConstraint(_in("conversion_fee_kind", CONVERSION_FEE_KINDS), name="conversion_fee_kind"),
        CheckConstraint("amends_end_date IS NULL OR contract_type = 'amendment'", name="amends_end_date"),
        CheckConstraint("value_cap IS NULL OR value_cap > 0", name="value_cap"),
        CheckConstraint(f"pricing_model IS NULL OR {_in('pricing_model', PRICING_MODELS)}", name="pricing_model"),
        CheckConstraint("contract_type = 'sow' OR pricing_model IS NULL", name="pricing_model_sow"),
        _tenant_fk("client_id", "clients", "fk_contracts_client_id_clients"),
        _tenant_fk("vendor_id", "vendors", "fk_contracts_vendor_id_vendors"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_contracts_legal_entity_id_legal_entities"),
        _tenant_fk("parent_contract_id", "contracts", "fk_contracts_parent_contract_id_contracts"),
        _tenant_fk("payment_terms_id", "payment_terms", "fk_contracts_payment_terms_id_payment_terms"),
        _tenant_fk("owner_id", "users", "fk_contracts_owner_id_users"),
        _tenant_fk("signed_file_id", "stored_files", "fk_contracts_signed_file_id_stored_files"),
        Index("ix_contracts_client_id", "client_id"),
        Index("ix_contracts_vendor_id", "vendor_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(20), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    # ---- Epic 16 (203_02)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    counterparty_kind: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'client'")
    )
    amends_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_cap: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    payment_terms_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    conversion_fee_kind: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'none'")
    )
    conversion_fee_value: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True)
    conversion_fee_waive_after_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placement_fee_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    reverse_charge: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    share_bgv_outcome: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renewal_lead_days: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signature_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- Epic 36 (300_03)
    pricing_model: Mapped[str | None] = mapped_column(String(20), nullable=True)


class ContractDocument(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "contract_documents"
    __table_args__ = (
        CheckConstraint(_in("kind", CONTRACT_DOCUMENT_KINDS), name="kind"),
        _tenant_fk("contract_id", "contracts", "fk_contract_documents_contract_id_contracts", "CASCADE"),
        _tenant_fk("file_id", "stored_files", "fk_contract_documents_file_id_stored_files"),
        Index("ix_contract_documents_contract_id", "contract_id"),
    )

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ContractMilestone(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "contract_milestones"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", MILESTONE_STATUSES), name="status"),
        CheckConstraint("amount > 0", name="amount"),
        _tenant_fk("contract_id", "contracts", "fk_contract_milestones_contract_id_contracts"),
        Index("ix_contract_milestones_contract_id", "contract_id"),
    )

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'planned'"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ContractReminder(UUIDPk, TenantMixin, Base):
    __tablename__ = "contract_reminders"
    __table_args__ = (
        UniqueConstraint("contract_id", "end_date", "lead_days"),
        _tenant_fk("contract_id", "contracts", "fk_contract_reminders_contract_id_contracts", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ================================================================ rate cards (FR-90)
class RateCard(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "rate_cards"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", RATE_CARD_STATUSES), name="status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="effective_range"),
        CheckConstraint("(client_id IS NULL) = is_template", name="template_has_no_client"),
        CheckConstraint("contract_id IS NULL OR client_id IS NOT NULL", name="contract_needs_client"),
        _tenant_fk("client_id", "clients", "fk_rate_cards_client_id_clients"),
        _tenant_fk("contract_id", "contracts", "fk_rate_cards_contract_id_contracts"),
        _tenant_fk("source_template_id", "rate_cards", "fk_rate_cards_source_template_id_rate_cards"),
        Index(
            "uq_rate_cards_version",
            "tenant_id",
            "client_id",
            text("COALESCE(contract_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "version",
            unique=True,
            postgresql_where=text("client_id IS NOT NULL"),
        ),
        Index("ix_rate_cards_client_id", "client_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateCardLine(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "rate_card_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("min_bill_rate >= 0 AND min_bill_rate <= max_bill_rate", name="bill_range"),
        CheckConstraint(
            "experience_min_years IS NULL OR experience_max_years IS NULL "
            "OR experience_min_years <= experience_max_years",
            name="experience_range",
        ),
        CheckConstraint(_in("rate_unit", RATE_UNITS), name="rate_unit"),
        _tenant_fk("rate_card_id", "rate_cards", "fk_rate_card_lines_rate_card_id_rate_cards", "CASCADE"),
        Index("ix_rate_card_lines_rate_card_id", "rate_card_id"),
    )

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rate_card_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False)
    role_key: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    experience_min_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    experience_max_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    min_bill_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    max_bill_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    rate_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    max_vendor_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    markup_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)


# ================================================================ rate checks (FR-91)
class RateCheck(UUIDPk, TenantMixin, Base):
    __tablename__ = "rate_checks"
    __table_args__ = (
        CheckConstraint(_in("subject_kind", RATE_CHECK_SUBJECT_KINDS), name="subject_kind"),
        CheckConstraint(_in("result", RATE_CHECK_RESULTS), name="result"),
        CheckConstraint(_in("exception_status", EXCEPTION_STATUSES), name="exception_status"),
        _tenant_fk("client_id", "clients", "fk_rate_checks_client_id_clients"),
        Index("ix_rate_checks_subject", "tenant_id", "subject_kind", "subject_id", "checked_at"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    input_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    rate_card_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rate_card_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    min_bill_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    max_bill_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    max_vendor_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    exception_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ purchase orders (FR-92, AD-32)
class PurchaseOrder(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "client_id", "po_number"),
        CheckConstraint(_in("status", PO_STATUSES), name="status"),
        CheckConstraint("valid_to >= valid_from", name="validity"),
        CheckConstraint("value_cap IS NULL OR value_cap > 0", name="value_cap"),
        CheckConstraint("hours_cap IS NULL OR hours_cap > 0", name="hours_cap"),
        CheckConstraint("value_cap IS NOT NULL OR hours_cap IS NOT NULL", name="some_cap"),
        _tenant_fk("client_id", "clients", "fk_purchase_orders_client_id_clients"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_purchase_orders_legal_entity_id_legal_entities"),
        _tenant_fk("contract_id", "contracts", "fk_purchase_orders_contract_id_contracts"),
        _tenant_fk("owner_id", "users", "fk_purchase_orders_owner_id_users"),
        _tenant_fk("file_id", "stored_files", "fk_purchase_orders_file_id_stored_files"),
        Index(
            "uq_purchase_orders_number_ci", "tenant_id", "client_id", text("lower(po_number)"), unique=True
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    po_number: Mapped[str] = mapped_column(String(60), nullable=False)
    currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    value_cap: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    hours_cap: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    # ---- Epic 16 (203_04)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    alert_thresholds_pct: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PoConsumption(UUIDPk, TenantMixin, Base):
    """Append-only (AD-32): written only by ``record_po_consumption``; DB trigger refuses UPDATE / DELETE."""

    __tablename__ = "po_consumptions"
    __table_args__ = (
        UniqueConstraint("source_doc_kind", "source_doc_id", "po_id"),
        CheckConstraint(_in("source_doc_kind", CONSUMPTION_DOC_KINDS), name="source_doc_kind"),
        _tenant_fk("client_id", "clients", "fk_po_consumptions_client_id_clients"),
        _tenant_fk("po_id", "purchase_orders", "fk_po_consumptions_po_id_purchase_orders"),
        Index("ix_po_consumptions_po_id", "po_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_doc_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    source_doc_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    hours: Mapped[Decimal] = mapped_column(HOURS, nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    exception_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PoAlert(UUIDPk, TenantMixin, Base):
    __tablename__ = "po_alerts"
    __table_args__ = (
        UniqueConstraint("po_id", "kind", "threshold", "crossing_seq"),
        CheckConstraint(_in("kind", PO_ALERT_KINDS), name="kind"),
        _tenant_fk("po_id", "purchase_orders", "fk_po_alerts_po_id_purchase_orders", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # threshold pct for ``threshold``; days-before for ``expiring``; 0 = became invalid / 1 = became valid for
    # ``validity``.
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    crossing_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
