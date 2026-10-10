"""Embedding tables owned by the AI service (Epic 5, story 5.2; AD-7). Migration 20261001_52.

Vectors are ``float4[]`` (384-dim, L2-normalised) so they work without pgvector. When the ``vector``
extension is available the migration also adds ``vec vector(384)`` + an HNSW index (``PgVectorIndex``);
those columns are deliberately not mapped here. Derived data: not audited. Tenant-owned, forced RLS.
Candidates without AI-processing consent have no rows.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, REAL, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin

EMBEDDING_KINDS = ("profile",)


class CandidateEmbedding(TenantMixin, Base):
    __tablename__ = "candidate_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_embeddings_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('profile')", name="kind"),
        Index("ix_candidate_embeddings_tenant_id_updated_at", "tenant_id", "updated_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True, server_default=text("'profile'"))
    vector: Mapped[list[float]] = mapped_column(ARRAY(REAL()), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DemandEmbedding(TenantMixin, Base):
    __tablename__ = "demand_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_embeddings_demand_id_demands",
            ondelete="CASCADE",
        ),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(ARRAY(REAL()), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
