# ruff: noqa: E501
"""Per-tenant integration credentials (unified platform spoke -> hub).

Platform table (no RLS, no tenant business data): one row per ``(tenant_id, service)`` holding the Fernet-
encrypted plaintext of a Public API key minted for that tenant's own ATS silo. Public API keys are stored
hash-only (``api_keys.key_hash``), so the ciphertext here is the ONLY way the internal service endpoint can
return the SAME key on later calls (idempotent). Keyed by the CENTRAL ``tenant_id`` across tenants, so it lives
in the platform DB (read / written through ``platform_session``), never a per-tenant silo. Added to
``core.tenancy.PLATFORM_TABLES``.

Reused by ``app.router.internal`` / ``app.services.internal_integration_service``:
``GET /api/v1/internal/tenant-ats-key`` finds-or-mints the calling tenant's ATS key.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


class TenantIntegrationCredential(Base):
    """Platform table: ``(tenant_id, service) -> Fernet(connector)`` ciphertext of a Public API key + its id."""

    __tablename__ = "tenant_integration_credentials"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    service: Mapped[str] = mapped_column(
        String(32), primary_key=True, nullable=False, server_default=text("'ats'")
    )
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
