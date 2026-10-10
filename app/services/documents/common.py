"""Shared helpers of the documents domain (Epic 22): time, principals, permissions, directory look-ups."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.permissions import Principal, forbidden

TEMPLATE_MANAGE = "document_template:manage"
DOCUMENT_READ = "document:read"
DOCUMENT_WRITE = "document:write"
DOCUMENT_HR = "document:hr"
DOCUMENT_FINANCE = "document:finance"
DOCUMENT_VERIFY = "document:verify"
REQUEST_CREATE = "signature_request:create"
REQUEST_CANCEL = "signature_request:cancel"

ESIGN_LINK_PURPOSE = "esign"
SIGNER_SUBJECT = "signer"
ERASED = "[erased]"


def now() -> datetime:
    return datetime.now(UTC)


def not_found(what: str = "Resource") -> NotFound:
    return NotFound(f"{what} not found")


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:  # pragma: no cover - tenant sessions only
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    if isinstance(p, Principal) and p.type == "user":
        return p
    return None


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = user_principal(ctx)
    return p.id if p else None


def has(ctx: RequestContext, permission: str) -> bool:
    p = user_principal(ctx)
    return p is not None and p.scope_of(permission) is not None


def require_perm(ctx: RequestContext, permission: str) -> None:
    if not has(ctx, permission):
        raise forbidden(permission)


def level_allowed(ctx: RequestContext, access_level: str) -> bool:
    """``hr`` needs ``document:hr``; ``finance`` needs ``document:finance``. System contexts pass."""
    if ctx.principal is None:
        return True
    if access_level == "hr":
        return has(ctx, DOCUMENT_HR)
    if access_level == "finance":
        return has(ctx, DOCUMENT_FINANCE)
    return True


# ---------------------------------------------------------------- directory (no cross-domain imports)
_candidates = table("candidates", column("id", Uuid()), column("full_name", String()))
_contacts = table(
    "contacts",
    column("id", Uuid()),
    column("name", String()),
    column("email", String()),
    column("client_id", Uuid()),
)


async def candidate_contact(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[str | None, str | None]:
    """(full name, primary email) of a candidate."""
    from app.services.candidates import pipeline_port

    info = (await pipeline_port.summaries(db, [candidate_id])).get(candidate_id, {})
    return info.get("full_name"), await pipeline_port.primary_email(db, candidate_id)


async def contact_info(
    db: AsyncSession, contact_id: uuid.UUID
) -> tuple[str | None, str | None, uuid.UUID | None]:
    row = (
        await db.execute(
            select(_contacts.c.name, _contacts.c.email, _contacts.c.client_id).where(
                _contacts.c.id == contact_id
            )
        )
    ).first()
    if row is None:
        return None, None, None
    return row.name, (str(row.email) if row.email else None), row.client_id


async def external_user(
    db: AsyncSession, kind: str, principal_id: uuid.UUID
) -> tuple[str, str, uuid.UUID] | None:
    """(display name, email, org id) of a client or vendor user."""
    from app.models.external_auth import ClientUser, VendorUser

    if kind == "client_user":
        cu = await db.get(ClientUser, principal_id)
        return (cu.display_name, str(cu.email), cu.client_id) if cu else None
    vu = await db.get(VendorUser, principal_id)
    return (vu.display_name, str(vu.email), vu.vendor_id) if vu else None


async def tenant_name(db: AsyncSession) -> str:
    from app.services import entity_refs

    return (await entity_refs.tenant(db, tenant_of(db))).name


def email_hint(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}{'*' * max(len(local) - 1, 2)}@{domain}"
