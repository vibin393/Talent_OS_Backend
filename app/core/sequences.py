"""Per-tenant human-readable IDs (AD-3), e.g. ``REQ-1056``, ``CAN-000123``.

Backed by ``tenant_sequences(tenant_id, key, value)`` (RLS-protected). The counter is advanced with an
``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` which takes a row lock on the counter row, so
concurrent callers for the same (tenant, key) serialize and receive distinct sequential values.
The tenant is taken from the session's tenant context (``app.tenant_id``), never from a parameter.
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, String, Uuid, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


class TenantSequence(Base):
    __tablename__ = "tenant_sequences"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))


_NEXT_VALUE_SQL = text(
    """
    INSERT INTO tenant_sequences (tenant_id, key, value)
    VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, :key, 1)
    ON CONFLICT (tenant_id, key) DO UPDATE SET value = tenant_sequences.value + 1
    RETURNING value
    """
)


async def next_value(session: AsyncSession, key: str) -> int:
    if not key or len(key) > 64:
        msg = "sequence key must be 1..64 characters"
        raise ValueError(msg)
    result = await session.execute(_NEXT_VALUE_SQL, {"key": key})
    return int(result.scalar_one())


async def next_human_id(session: AsyncSession, key: str, prefix: str | None = None, width: int = 4) -> str:
    """Return the next ``<PREFIX>-<zero-padded n>`` for ``key`` in the session's tenant.

    Must be called inside a ``tenant_session``; without tenant context the insert violates the
    NOT NULL / RLS check and raises.
    """
    value = await next_value(session, key)
    return f"{prefix or key}-{value:0{width}d}"
