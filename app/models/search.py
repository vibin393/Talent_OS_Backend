"""Search read model (Epic 5, story 5.1): ``candidate_search_docs``, one row per candidate.

Search-owned projection of candidate data, rebuilt from ``candidates`` service projections by
``app.services.search.projection.refresh_docs`` (events -> Celery). Not audited (derived data). Tenant-owned
with forced RLS (migration 20261001_51). ``doc`` / ``name_norm`` are computed in SQL on upsert:
``doc = A name/headline/title || B skills || C employers/certs/tags || D resume text`` using the
``talent_en`` text-search configuration (english + unaccent).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin


class CandidateSearchDoc(TenantMixin, Base):
    __tablename__ = "candidate_search_docs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_search_docs_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        Index(
            "ix_candidate_search_docs_doc",
            "doc",
            postgresql_using="gin",
            postgresql_where=text("NOT is_archived"),
        ),
        Index(
            "ix_candidate_search_docs_name_norm_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_candidate_search_docs_email_trgm", text("(email::text) gin_trgm_ops"), postgresql_using="gin"
        ),
        Index("ix_candidate_search_docs_tenant_id_phone_e164", "tenant_id", "phone_e164"),
        Index("ix_candidate_search_docs_tenant_id_human_id", "tenant_id", "human_id"),
        Index("ix_candidate_search_docs_skill_ids", "skill_ids", postgresql_using="gin"),
        Index("ix_candidate_search_docs_tags", "tags", postgresql_using="gin"),
        Index("ix_candidate_search_docs_work_auth", "work_auth", postgresql_using="gin"),
        Index(
            "ix_candidate_search_docs_submitted_client_ids", "submitted_client_ids", postgresql_using="gin"
        ),
        Index("ix_candidate_search_docs_tenant_id_country_city", "tenant_id", "country", "city"),
        Index("ix_candidate_search_docs_tenant_id_experience_years", "tenant_id", "experience_years"),
        Index("ix_candidate_search_docs_tenant_id_notice_days", "tenant_id", "notice_days"),
        Index("ix_candidate_search_docs_tenant_id_available_from", "tenant_id", "available_from"),
        Index("ix_candidate_search_docs_tenant_id_expected_comp_inr", "tenant_id", "expected_comp_inr"),
        Index("ix_candidate_search_docs_tenant_id_owner_id", "tenant_id", "owner_id"),
        Index(
            "ix_candidate_search_docs_tenant_id_candidate_updated_at",
            "tenant_id",
            text("candidate_updated_at DESC"),
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_employer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    current_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    skill_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    skill_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    certifications: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    tags: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    work_auth: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    experience_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    # Expected compensation normalised at write time to annual INR (original amount/currency for display).
    expected_comp_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_comp_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_comp_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    expected_comp_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notice_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    submitted_client_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    ai_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    candidate_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Latest resume text (<= 200 KB): source of the D-weight lexemes and of ``ts_headline`` snippets.
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
