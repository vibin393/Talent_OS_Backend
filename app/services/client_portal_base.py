"""Low-level helpers of the Client Portal content (Epic 18), importable by every ``services/<domain>/portal.py``
``client_`` function (no upward imports): the calling Client User, display names and demand scope checks."""

import hashlib
import uuid
from typing import Any

from app.core.client_scope import visible_demand_ids, visible_placement_ids
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound
from app.core.settings import get_settings


def client_of(ctx: RequestContext) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """``(tenant_id, client_id, client_user_id)`` of a Client User request (403 otherwise)."""
    principal: Any = ctx.principal
    if getattr(principal, "type", None) != "client_user":
        raise Forbidden("Client portal only")
    tenant_id, client_id, me = principal.tenant_id, principal.org_id, principal.id
    if tenant_id is None or client_id is None or me is None:
        raise Forbidden("Client portal only")
    return tenant_id, client_id, me


def has_permission(ctx: RequestContext, key: str) -> bool:
    return key in (getattr(ctx.principal, "portal_permissions", None) or ())


def display_name(full_name: str | None) -> str:
    """First name + last initial (the P1 scorecard / secure-link convention)."""
    parts = (full_name or "").split()
    if not parts:
        return "Candidate"
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


async def demand_in_scope(ctx: RequestContext, db: Any, demand_id: uuid.UUID) -> bool:
    scope = await visible_demand_ids(ctx, db)
    return scope is None or demand_id in scope


async def require_demand_scope(
    ctx: RequestContext, db: Any, demand_id: uuid.UUID, what: str = "Demand"
) -> None:
    if not await demand_in_scope(ctx, db, demand_id):
        raise NotFound(f"{what} not found")


async def placement_scope(ctx: RequestContext, db: Any) -> frozenset[uuid.UUID] | None:
    return await visible_placement_ids(ctx, db)


def ip_hash(ip: str | None) -> str | None:
    if not ip:
        return None
    salt = get_settings().app_env
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()
