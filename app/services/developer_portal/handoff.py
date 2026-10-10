# ruff: noqa: E501, D401
"""One-time sign-in into a sandbox (Epic 74, story 74.3, spec 0.4). ``open_sandbox`` stores a single-use token (hashed,
60 s, bound to the developer and the sandbox) and returns ``/developers/sandbox/enter#t=...``; ``consume_handoff``
pops it and opens a normal ``tos_session`` for the developer's sandbox user (the developer's MFA covers it)."""

import secrets
import uuid

from app.core.database import platform_session
from app.core.errors import ConflictError, Gone
from app.core.security import csrf_token_for, hash_token
from app.core.tenancy import is_sandbox
from app.models.developer import DeveloperSandbox
from app.schemas.developer import HandoffOut
from app.services import auth_service
from app.services.developer_portal import audit_log
from app.services.developer_portal.common import kv_get, kv_set
from app.services.developer_portal.owner import Owner
from app.services.developer_portal.sandboxes import get_owned, member_user

TTL_SECONDS = 60
PREFIX = "dev:handoff:"


async def open_sandbox(owner: Owner, sandbox_id: uuid.UUID) -> HandoffOut:
    owner.require("viewer")
    row = await get_owned(owner, sandbox_id, require_ready=True)
    user_id = await member_user(owner, row)
    token = secrets.token_urlsafe(32)
    await kv_set(PREFIX + hash_token(token), {
        "sandbox_id": str(row.id), "tenant_id": str(row.tenant_id), "user_id": str(user_id),
        "account_id": str(owner.account_id) if owner.account_id else None, "owner_user_id": str(owner.user_id) if owner.user_id else None,
    }, TTL_SECONDS)  # fmt: skip
    if owner.kind == "partner_org":
        await audit_log.record("sandbox.opened", partner_org_id=owner.id, account_id=owner.account_id, target_type="sandbox", target_id=row.id)
    return HandoffOut(url=f"/developers/sandbox/enter#t={token}", expires_in=TTL_SECONDS)


async def consume_handoff(token: str, *, account_id: uuid.UUID | None, ip: str | None, user_agent: str | None) -> tuple[str, str, int]:
    """Returns ``(session token, csrf token, max_age)`` of the new sandbox session; 410 ``handoff_expired`` when the token
    was used, expired or belongs to another developer."""
    data = await kv_get(PREFIX + hash_token(token), pop=True)
    if data is None:
        raise Gone("This sign-in link has expired. Open the sandbox again.", code="handoff_expired")
    if account_id is not None and data.get("account_id") not in (None, str(account_id)):
        raise Gone("This sign-in link has expired. Open the sandbox again.", code="handoff_expired")
    tenant_id = uuid.UUID(data["tenant_id"])
    async with platform_session() as db:
        row = await db.get(DeveloperSandbox, uuid.UUID(data["sandbox_id"]))
        if row is None or row.tenant_id != tenant_id or row.status != "ready":
            raise Gone("This sign-in link has expired. Open the sandbox again.", code="handoff_expired")
    if not await is_sandbox(tenant_id):
        raise ConflictError("Only sandbox tenants can be opened here", code="sandbox_only")
    async with platform_session() as db:
        issued = await auth_service.create_session(
            db, principal_type="user", principal_id=uuid.UUID(data["user_id"]), tenant_id=tenant_id, ip=ip, user_agent=user_agent,
        )  # fmt: skip
    return issued.token, csrf_token_for(issued.token), issued.max_age


__all__ = ["TTL_SECONDS", "consume_handoff", "open_sandbox"]
