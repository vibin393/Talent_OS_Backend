"""Secure links (NFR-17): single-purpose, expiring, use-limited, revocable links without PII in the URL.

Minimal implementation of the Epic 6 A5 primitive, built with Epic 12 (consent requests, DSR exports,
tenant exports). Epic 6 extends it (public view models, screening/feedback purposes) -- it must reuse these
tables and this API rather than create new ones.

* ``secure_links`` (tenant-owned, RLS): purpose, subject, ``token_hash`` (SHA-256 bytes), expiry, uses.
* ``secure_link_lookups`` (platform table, no PII): ``token_hash -> (tenant_id, link_id)`` so a public
  request can open the right tenant session before loading the link.
* The raw token (``secrets.token_urlsafe(32)``, 256 bits) is returned once by ``issue`` and never stored.
* Public URL: ``{FRONTEND_URL}/l/{token}`` (path form kept for already-sent emails; the log redactor masks it).
"""

import dataclasses
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, LargeBinary, String, Uuid, column, func, select, table, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core import audit
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError, NotFound
from app.core.models import Base, TenantMixin, UUIDPk
from app.core.settings import get_settings


class SecureLink(UUIDPk, TenantMixin, Base):
    __tablename__ = "secure_links"

    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SecureLinkLookup(Base):
    """Platform table: token hash -> tenant + link id. No PII."""

    __tablename__ = "secure_link_lookups"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


_tenants = table("tenants", column("id"), column("status"))
OFFBOARDING_PURPOSES: frozenset[str] = frozenset({"tenant_export"})


class LinkGone(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - mirrors HTTP 410
    status_code = 410
    code = "link_expired"
    default_message = "This link is no longer valid"


def _now() -> datetime:
    return datetime.now(UTC)


def hash_link_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def link_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/l/{token}"


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedLink:
    id: uuid.UUID
    tenant_id: uuid.UUID
    purpose: str
    subject_type: str
    subject_id: uuid.UUID
    expires_at: datetime
    max_uses: int
    use_count: int
    meta: dict[str, Any]


async def issue(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    subject_type: str,
    subject_id: uuid.UUID,
    ttl: timedelta,
    max_uses: int = 1,
    metadata: dict[str, Any] | None = None,
    created_by: uuid.UUID | None = None,
) -> tuple[SecureLink, str]:
    """Create a link in ``db`` (a tenant session for ``tenant_id``); returns ``(link, raw_token)``."""
    token = secrets.token_urlsafe(32)
    digest = hash_link_token(token)
    link = SecureLink(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        purpose=purpose,
        subject_type=subject_type,
        subject_id=subject_id,
        token_hash=digest,
        expires_at=_now() + ttl,
        max_uses=max_uses,
        use_count=0,
        created_by=created_by,
        meta=metadata or {},
    )
    db.add(link)
    db.add(SecureLinkLookup(token_hash=digest, tenant_id=tenant_id, link_id=link.id))
    await db.flush()
    return link, token


def _gone(code: str, message: str) -> LinkGone:
    return LinkGone(message, code=code)


async def resolve(token: str, expected_purposes: tuple[str, ...] | frozenset[str]) -> ResolvedLink:
    """Look ``token`` up and validate it. Viewing never consumes. Raises 404 ``link_invalid`` / 410."""
    if not token or len(token) > 200:
        raise NotFound("Link not found", code="link_invalid")
    digest = hash_link_token(token)
    async with platform_session() as pdb:
        lookup = await pdb.get(SecureLinkLookup, digest)
        status = (
            (await pdb.execute(select(_tenants.c.status).where(_tenants.c.id == lookup.tenant_id))).scalar()
            if lookup is not None
            else None
        )
    if lookup is None:
        raise NotFound("Link not found", code="link_invalid")
    # Suspended / deleted tenants serve no public links (FR-70); offboarding tenants only their export.
    if status != "active" and not (status == "offboarding" and set(expected_purposes) & OFFBOARDING_PURPOSES):
        raise _gone("link_unavailable", "This link is currently unavailable")
    async with tenant_session(lookup.tenant_id) as db:
        link = await db.get(SecureLink, lookup.link_id)
        if link is None or link.purpose not in expected_purposes:
            raise NotFound("Link not found", code="link_invalid")
        if status != "active" and link.purpose not in OFFBOARDING_PURPOSES:
            raise _gone("link_unavailable", "This link is currently unavailable")
        now = _now()
        error: LinkGone | None = None
        if link.revoked_at is not None:
            error = _gone("link_revoked", "This link was revoked")
        elif link.use_count >= link.max_uses:
            error = _gone("link_used", "This link was already used")
        elif link.expires_at <= now:
            error = _gone("link_expired", "This link has expired")
        audit.record(
            "secure_link.rejected" if error else "secure_link.viewed",
            "secure_link",
            link.id,
            session=db,
            actor_type="system",
            actor_id=None,
            purpose=link.purpose,
            reason=error.code if error else None,
        )
        resolved = ResolvedLink(
            id=link.id,
            tenant_id=link.tenant_id,
            purpose=link.purpose,
            subject_type=link.subject_type,
            subject_id=link.subject_id,
            expires_at=link.expires_at,
            max_uses=link.max_uses,
            use_count=link.use_count,
            meta=dict(link.meta or {}),
        )
    if error is not None:
        raise error
    return resolved


async def consume(db: AsyncSession, link_id: uuid.UUID) -> None:
    """Atomically use one use of the link (``db``: tenant session). Racing callers: one wins, others 410."""
    now = _now()
    result = await db.execute(
        update(SecureLink)
        .where(
            SecureLink.id == link_id,
            SecureLink.revoked_at.is_(None),
            SecureLink.expires_at > now,
            SecureLink.use_count < SecureLink.max_uses,
        )
        .values(
            use_count=SecureLink.use_count + 1,
            last_used_at=now,
            consumed_at=func.coalesce(SecureLink.consumed_at, now),
        )
        .returning(SecureLink.purpose)
    )
    purpose = result.scalar_one_or_none()
    if purpose is None:
        raise _gone("link_used", "This link was already used")
    audit.record(
        "secure_link.used", "secure_link", link_id, session=db, actor_type="system", actor_id=None, purpose=purpose
    )


async def revoke(db: AsyncSession, link_id: uuid.UUID) -> bool:
    result = await db.execute(
        update(SecureLink)
        .where(SecureLink.id == link_id, SecureLink.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return bool(result.rowcount)


async def revoke_for_subject(
    db: AsyncSession, subject_type: str, subject_id: uuid.UUID, purpose: str | None = None
) -> int:
    stmt = update(SecureLink).where(
        SecureLink.subject_type == subject_type,
        SecureLink.subject_id == subject_id,
        SecureLink.revoked_at.is_(None),
    )
    if purpose is not None:
        stmt = stmt.where(SecureLink.purpose == purpose)
    result = await db.execute(stmt.values(revoked_at=_now()))
    return int(result.rowcount)


async def links_for_subject(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> list[SecureLink]:
    stmt = select(SecureLink).where(SecureLink.subject_type == subject_type, SecureLink.subject_id == subject_id)
    return list((await db.execute(stmt)).scalars())
