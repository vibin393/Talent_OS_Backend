# ruff: file-ignore[line-too-long]
"""Sales CRM (Epic 15, FR-83..FR-87): settings, reason codes, lead sources, leads, status history,
opportunities + role lines and the Won-conversion record links.

Migrations: band 202, branch ``p2_crm`` (``20261004_202_01..04``). Every table is tenant RLS + internal-only org
RLS (portal sessions see nothing). References to P1 tables use composite ``(tenant_id, x_id)`` foreign keys.
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

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

LEAD_STATUSES = ("new", "contacted", "qualified", "converted", "disqualified")
OPPORTUNITY_STAGES = ("qualification", "proposal", "negotiation", "won", "lost")
OPEN_STAGES = ("qualification", "proposal", "negotiation")
SERVICES = ("contract_staffing", "permanent_placement", "project", "other")
REASON_KINDS = ("lead_disqualify", "opportunity_lost", "reopen")
ENTITY_KINDS = ("lead", "opportunity")
RECORD_KINDS = ("client", "contract", "rate_card", "demand")
RATE_UNITS = ("hour", "day", "month", "year")
DEFAULT_STAGE_PROBABILITIES: dict[str, int] = {
    "qualification": 10,
    "proposal": 40,
    "negotiation": 70,
    "won": 100,
    "lost": 0,
}
MONEY = Numeric(16, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CrmSettings(Timestamps, Base):
    """One row per tenant, created lazily."""

    __tablename__ = "crm_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    stage_probabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB(),
        nullable=False,
        server_default='{"qualification":10,"proposal":40,"negotiation":70,"won":100,"lost":0}',
    )
    default_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))


class CrmReasonCode(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "crm_reason_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "code"),
        CheckConstraint(_in("kind", REASON_KINDS), name="kind"),
    )

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CrmLeadSource(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "crm_lead_sources"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Lead(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "leads"
    __pii_fields__ = ("contact_name", "contact_email", "contact_phone")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", LEAD_STATUSES), name="status"),
        CheckConstraint("estimated_positions IS NULL OR estimated_positions >= 0", name="positions"),
        _tfk("owner_id", "users", "fk_leads_owner_id_users"),
        _tfk("converted_client_id", "clients", "fk_leads_converted_client_id_clients"),
        _tfk("converted_contact_id", "contacts", "fk_leads_converted_contact_id_contacts"),
        Index(
            "ix_leads_company_name_trgm", text("(company_name::text) gin_trgm_ops"), postgresql_using="gin"
        ),
        Index("ix_leads_tenant_domain", "tenant_id", "website_domain"),
        Index("ix_leads_tenant_gstin", "tenant_id", "gstin"),
        Index("ix_leads_tenant_status_owner", "tenant_id", "status", "owner_id"),
        Index("ix_leads_tenant_source_created", "tenant_id", "source_code", "created_at"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    company_name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    estimated_roles: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    estimated_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_annual_revenue: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'new'"))
    disqualify_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    disqualify_note: Mapped[str | None] = mapped_column(Text(), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    converted_client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    converted_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duplicate_ack_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CrmStatusHistory(UUIDPk, TenantMixin, Base):
    __tablename__ = "crm_status_history"
    __table_args__ = (
        CheckConstraint(_in("entity_kind", ENTITY_KINDS), name="entity_kind"),
        Index("ix_crm_status_history_entity", "tenant_id", "entity_kind", "entity_id", "at"),
    )

    entity_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_value: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_value: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(Text(), nullable=True)
    competitor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Opportunity(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("stage", OPPORTUNITY_STAGES), name="stage"),
        CheckConstraint(_in("service", SERVICES), name="service"),
        CheckConstraint("probability_pct >= 0 AND probability_pct <= 100", name="probability"),
        CheckConstraint("estimated_revenue >= 0", name="revenue"),
        _tfk("client_id", "clients", "fk_opportunities_client_id_clients"),
        _tfk("primary_contact_id", "contacts", "fk_opportunities_primary_contact_id_contacts"),
        _tfk("lead_id", "leads", "fk_opportunities_lead_id_leads"),
        _tfk("owner_id", "users", "fk_opportunities_owner_id_users"),
        _tfk("account_manager_id", "users", "fk_opportunities_account_manager_id_users"),
        Index("ix_opportunities_stage_close", "tenant_id", "stage", "expected_close_date"),
        Index("ix_opportunities_owner_stage", "tenant_id", "owner_id", "stage"),
        Index("ix_opportunities_client", "tenant_id", "client_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    primary_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    service: Mapped[str] = mapped_column(String(24), nullable=False)
    estimated_revenue: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    probability_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"))
    probability_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    expected_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_manager_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'qualification'"))
    stage_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    lost_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lost_competitor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lost_note: Mapped[str | None] = mapped_column(Text(), nullable=True)
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpportunityLine(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "opportunity_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("positions >= 1", name="positions"),
        CheckConstraint(f"rate_unit IS NULL OR {_in('rate_unit', RATE_UNITS)}", name="rate_unit"),
        _tfk(
            "opportunity_id", "opportunities", "fk_opportunity_lines_opportunity_id_opportunities", "CASCADE"
        ),
        Index("ix_opportunity_lines_opportunity", "opportunity_id", "position"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    positions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    expected_bill_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    experience_min_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    experience_max_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class CrmRecordLink(UUIDPk, TenantMixin, Base):
    """Records created by a Won conversion (polymorphic ``record_id``, no FK)."""

    __tablename__ = "crm_record_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "opportunity_id", "record_kind", "record_id"),
        CheckConstraint(_in("record_kind", RECORD_KINDS), name="record_kind"),
        _tfk("opportunity_id", "opportunities", "fk_crm_record_links_opportunity_id_opportunities"),
        Index("ix_crm_record_links_record", "tenant_id", "record_kind", "record_id"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    record_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
