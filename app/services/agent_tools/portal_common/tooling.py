# ruff: noqa: E501, D401
"""Shared plumbing of the ``portal_*`` agent tools (Epic 77 A3). Not a tool module.

``acting(tctx, kind)`` resolves who the tool acts for:

* during a run (``execution='agent'``) the context principal is the agent with Effective Rights = AgentSpec ceiling ∩
  the owner's portal permissions; the tool presents the **owner** (Client User / Vendor User / Candidate) with exactly
  those rights to the wrapped portal service, so delegations (``client_user_scopes``), FR-80 projections and self-scope
  apply as in the portal. The session is the executor's org-scoped session.
* on confirm (``execution='copilot_confirm'``) the principal is the live portal principal (never a staff identity,
  AD-89); the tool opens an org-scoped session of its own for the write.

An owner that cannot be resolved raises ``NotFound`` (the same answer as "nothing visible")."""

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.errors import NotFound
from app.core.field_visibility import scan_payload
from app.core.permissions import Principal

OWNER_EPIC = "77"
PRINCIPAL_TYPES: dict[str, str] = {"client": "client_user", "vendor": "vendor_user", "candidate": "candidate"}
ORG_KINDS: dict[str, str] = {"client": "client", "vendor": "vendor", "candidate": "worker"}
KIND_OF_TYPE: dict[str, str] = {v: k for k, v in PRINCIPAL_TYPES.items()}
_runs = table(
    "agent_runs",
    column("id"),
    column("owner_principal_type"),
    column("owner_principal_id"),
    column("owner_org_kind"),
    column("owner_org_id"),
)


async def run_owner(tenant_id: uuid.UUID, run_id: uuid.UUID) -> tuple[str, uuid.UUID, str, uuid.UUID] | None:
    """``(principal_type, principal_id, org_kind, org_id)`` of a portal-owned run (internal read: ``agent_runs`` is
    internal-only under org RLS)."""
    async with tenant_session(tenant_id) as idb:
        row = (
            await idb.execute(
                select(
                    _runs.c.owner_principal_type,
                    _runs.c.owner_principal_id,
                    _runs.c.owner_org_kind,
                    _runs.c.owner_org_id,
                ).where(_runs.c.id == run_id)
            )
        ).first()
    if row is None or row[0] in (None, "user") or row[1] is None or row[3] is None:
        return None
    return str(row[0]), row[1], str(row[2]), row[3]


SCAN_KIND = {"client": "client_user", "vendor": "vendor_user", "candidate": "worker"}


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclasses.dataclass(frozen=True, slots=True)
class Acting:
    kind: str
    tenant_id: uuid.UUID
    principal: (
        Principal  # the portal principal (type client_user / vendor_user / candidate) with effective rights
    )
    ctx: RequestContext
    run_path: bool

    @property
    def org_id(self) -> uuid.UUID:
        assert self.principal.org_id is not None
        return self.principal.org_id

    def has(self, key: str) -> bool:
        return key in self.principal.portal_permissions


async def acting(tctx: Any, kind: str) -> Acting:
    req: RequestContext = tctx.request
    principal = req.principal
    if not isinstance(principal, Principal) or req.tenant_id is None:
        raise NotFound("Resource not found")
    expected = PRINCIPAL_TYPES[kind]
    rights = frozenset(principal.permissions)
    if principal.type == expected:  # confirm path: the live portal principal
        owner = dataclasses.replace(principal, portal_permissions=rights)
        return Acting(kind, req.tenant_id, owner, dataclasses.replace(req, principal=owner), run_path=False)
    if principal.type != "agent" or tctx.run_id is None:
        raise NotFound("Resource not found")
    found = await run_owner(req.tenant_id, tctx.run_id)
    if (
        found is None
        or KIND_OF_TYPE.get(found[0]) != kind
        or found[2] != ORG_KINDS[kind]
        or found[3] != principal.org_id
    ):
        raise NotFound("Resource not found")
    owner = Principal(
        type=expected,
        id=found[1],
        tenant_id=req.tenant_id,
        org_id=found[3],
        portal_permissions=rights,  # type: ignore[arg-type]
    )
    return Acting(kind, req.tenant_id, owner, dataclasses.replace(req, principal=owner), run_path=True)


@contextlib.asynccontextmanager
async def session(tctx: Any, act: Acting) -> AsyncIterator[AsyncSession]:  # noqa: ARG001 - kept for the call signature
    """The org-scoped session for the wrapped portal call."""
    # Always a session of its own scoped to the owner's organisation: a Draft preview runs on the runtime's internal
    # session, so the tool never trusts ``tctx.db`` for scope (fail closed).
    async with tenant_session(act.tenant_id, org=OrgScope.of(ORG_KINDS[act.kind], act.org_id)) as db:
        yield db


def guard(kind: str, data: BaseModel, *, allow: frozenset[str] = frozenset()) -> BaseModel:
    """FR-80: a tool output never carries a forbidden field for its portal (same scanner as ``portal_guard_route``)."""
    hits = scan_payload(data.model_dump(mode="json"), SCAN_KIND[kind], allow=allow)
    if hits:
        msg = f"portal tool output leaks forbidden fields: {hits}"
        raise RuntimeError(msg)
    return data


def candidate_principal(act: Acting) -> Any:
    from app.services.portal.common import CandidatePrincipal

    return CandidatePrincipal(
        candidate_id=act.principal.id,
        tenant_id=act.tenant_id,
        session_id=act.principal.session_id or act.principal.id,
    )


__all__ = [
    "OWNER_EPIC",
    "SCAN_KIND",
    "Acting",
    "In",
    "Out",
    "acting",
    "candidate_principal",
    "guard",
    "session",
]
