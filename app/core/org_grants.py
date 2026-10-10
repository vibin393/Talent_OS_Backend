"""Explicit organization grants (AD-26): give one external organization (client / vendor / worker) visibility
of one internal subject (e.g. an approval request it must decide). Read by ``enable_org_rls(grants=...)``.

``org_grants(org_kind, org_id, subject_kind, subject_id, granted_by_kind, granted_by_id, expires_at)``,
UNIQUE ``(tenant_id, org_kind, org_id, subject_kind, subject_id)``; org RLS ``self_org``. Grants are kept
after the subject completes (history).
"""

import uuid
from datetime import datetime

from sqlalchemy import column, delete, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef

ORG_KINDS = ("client", "vendor", "worker")

_org_grants = table(
    "org_grants",
    column("id"),
    column("tenant_id"),
    column("org_kind"),
    column("org_id"),
    column("subject_kind"),
    column("subject_id"),
    column("granted_by_kind"),
    column("granted_by_id"),
    column("expires_at"),
)


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "org grants need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _kind(org_kind: str) -> str:
    if org_kind not in ORG_KINDS:
        msg = f"unknown org kind {org_kind!r}"
        raise ValueError(msg)
    return org_kind


async def grant(
    db: AsyncSession,
    org_kind: str,
    org_id: uuid.UUID,
    subject_kind: str,
    subject_id: uuid.UUID,
    granted_by: ActorRef,
    *,
    expires_at: datetime | None = None,
) -> None:
    """Idempotent: an existing grant is kept (its expiry is not shortened)."""
    await db.execute(
        pg_insert(_org_grants)
        .values(
            id=uuid.uuid4(),
            tenant_id=_tenant(db),
            org_kind=_kind(org_kind),
            org_id=org_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            granted_by_kind=granted_by.kind,
            granted_by_id=granted_by.id,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "org_kind", "org_id", "subject_kind", "subject_id"])
    )


async def revoke(
    db: AsyncSession, org_kind: str, org_id: uuid.UUID, subject_kind: str, subject_id: uuid.UUID
) -> None:
    await db.execute(
        delete(_org_grants).where(
            _org_grants.c.org_kind == _kind(org_kind),
            _org_grants.c.org_id == org_id,
            _org_grants.c.subject_kind == subject_kind,
            _org_grants.c.subject_id == subject_id,
        )
    )


async def has_grant(
    db: AsyncSession, org_kind: str, org_id: uuid.UUID, subject_kind: str, subject_id: uuid.UUID
) -> bool:
    row = (
        await db.execute(
            select(_org_grants.c.id).where(
                _org_grants.c.org_kind == _kind(org_kind),
                _org_grants.c.org_id == org_id,
                _org_grants.c.subject_kind == subject_kind,
                _org_grants.c.subject_id == subject_id,
            )
        )
    ).first()
    return row is not None
