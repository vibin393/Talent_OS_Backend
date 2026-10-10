"""Tenant settings hub storage (Epic 12, FR-74; migration 20261001_124).

``tenant_settings`` holds one row per tenant with jsonb sections (``company``, ``regional``, ``id_prefixes``,
``link_expiries``). Tenant-owned (RLS), audited. Settings owned by other domains stay in their domains.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps


class TenantSettings(Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tenant_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    company: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    regional: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    id_prefixes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    link_expiries: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
