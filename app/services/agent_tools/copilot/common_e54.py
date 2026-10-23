# ruff: file-ignore[line-too-long]
"""Shared helpers of the Epic 54 Copilot tools (not a tool module: the loader imports only ``tools_*.py``).

Every read runs under ``agent_runtime_service.scope_context`` (Effective Rights presented as the Accountable Owner), so
scope and masking are exactly the user's; an invisible record raises ``NotFound`` like a missing one (NFR-46).
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.errors import NotFound
from app.core.permissions import Principal, mask_compensation
from app.services import agent_runtime_service as runtime
from app.services import entity_refs

OWNER_EPIC = "54"

# Link prefixes of the staff app per record type (the Copilot answer may link only these, validated against refs).
URL_PREFIX: dict[str, str] = {
    "candidate": "/candidates",
    "demand": "/demands",
    "client": "/clients",
    "placement": "/placements",
    "interview": "/interviews",
    "worker": "/workers",
    "project": "/projects",
}


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


def principal_of(ctx: Any) -> Principal | None:
    principal = ctx.principal
    return principal if isinstance(principal, Principal) else None


def masked(data: Any, ctx: Any) -> Any:
    """Compensation masking for raw values the domain did not mask already (no-op with ``compensation:view``)."""
    principal = principal_of(ctx)
    if principal is None:
        return data
    return mask_compensation(data, principal)


def url_for(kind: str, record_id: uuid.UUID | str | None) -> str | None:
    prefix = URL_PREFIX.get(kind)
    return f"{prefix}/{record_id}" if prefix and record_id else None


async def require_readable(tctx: Any, entity_type: str, entity_id: uuid.UUID) -> None:
    if not await entity_refs.can_read(scoped(tctx), tctx.db, entity_type, entity_id):
        raise NotFound("Resource not found")


def as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


__all__ = [
    "OWNER_EPIC",
    "URL_PREFIX",
    "In",
    "Out",
    "as_uuid",
    "masked",
    "plural",
    "principal_of",
    "require_readable",
    "scoped",
    "url_for",
]
