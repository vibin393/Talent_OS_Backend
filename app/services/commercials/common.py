"""Shared helpers of the commercials domain (Epic 16): tenant / clock / lookups / timeline."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.fiscal import local_date
from app.models.clients import Client
from app.models.legal_entities import LegalEntity

DEFAULT_TZ = "Asia/Kolkata"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "commercials needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def now() -> datetime:
    return datetime.now(UTC)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if getattr(principal, "type", None) == "user":
        return getattr(principal, "id", None)
    return None


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def require_client(db: AsyncSession, client_id: uuid.UUID) -> Client:
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise NotFound("Client not found")
    return client


async def legal_entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntity:
    entity = (
        await db.execute(select(LegalEntity).where(LegalEntity.id == legal_entity_id))
    ).scalar_one_or_none()
    if entity is None:
        raise invalid("legal_entity_invalid", "Unknown legal entity", legal_entity_id=str(legal_entity_id))
    return entity


async def le_today(db: AsyncSession, legal_entity_id: uuid.UUID | None, at: datetime | None = None) -> date:
    """Business "today" in the Legal Entity's timezone (AD-14)."""  # ruff: ignore[non-imperative-mood]
    tz = DEFAULT_TZ
    if legal_entity_id is not None:
        found = (
            await db.execute(select(LegalEntity.timezone).where(LegalEntity.id == legal_entity_id))
        ).scalar_one_or_none()
        tz = found or DEFAULT_TZ
    return local_date(at or now(), tz)


async def timeline(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    from app.services import activity_service

    await activity_service.add_system_entry(ctx, db, entity_type, entity_id, body, data)
