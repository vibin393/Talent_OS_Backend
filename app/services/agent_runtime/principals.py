# ruff: noqa: E501, D401
"""Agent principals and Effective Rights (Epic 53, AD-60, FR-265, NFR-46).

* ``ensure_agent_principal`` -- the tenant's non-human identity for an agent key (lazily created; identity only).
* ``effective_principal`` -- ``Principal(type="agent")`` whose permissions are, per key, the narrower scope of the
  Accountable Owner's live permission and the AgentSpec ``permission_ceiling`` (keys present in both), with the owner's
  teams and business unit. An inactive / missing owner raises ``OwnerUnavailable``.
* ``tool_context_request`` -- the ``RequestContext`` tools run under (actor ``agent``, on behalf of the owner).

Epic 66 (§0.15): for ``origin='external'`` (MCP) runs the owner's live permissions are first narrowed to the delegated
token's scopes (``oauth_scopes.permissions_for``); for portal owners (``owner_principal_type`` <> ``user``) Effective
Rights = the AgentSpec ceiling ∩ the live portal principal's ``portal_permissions`` (a deactivated portal user, or one
moved to another organization, raises ``OwnerUnavailable``).
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy import column, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import AgentSpec
from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.models.agent_runtime import AgentPrincipal, AgentRun
from app.models.external_auth import ClientUser, VendorUser
from app.services import entity_refs

_candidates = table("candidates", column("id"), column("erased_at"))


# Epic 77 (spec A2): the implicit portal permissions of a Candidate owner (tenant enablement + active consent) --
# registered by ``portal_copilot`` (P5 code never imported here); none registered = no rights (fail closed).
CandidateRights = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[frozenset[str]]]
_candidate_rights: list[CandidateRights] = []


def register_candidate_rights(fn: CandidateRights) -> None:
    _candidate_rights[:] = [fn]


class OwnerUnavailable(Exception):  # noqa: N818 - domain name
    """The Accountable Owner is missing or inactive (the run fails ``owner_unavailable`` + fallback)."""


def narrower(a: Scope, b: Scope) -> Scope:
    return a if a.rank <= b.rank else b


def intersect(owner: Mapping[str, Scope], ceiling: Mapping[str, Scope]) -> dict[str, Scope]:
    return {k: narrower(Scope(owner[k]), Scope(ceiling[k])) for k in owner if k in ceiling}


async def ensure_agent_principal(db: AsyncSession, tenant_id: uuid.UUID, spec: AgentSpec) -> AgentPrincipal:
    row = (
        await db.execute(select(AgentPrincipal).where(AgentPrincipal.agent_key == spec.key))
    ).scalar_one_or_none()
    if row is None:
        await db.execute(
            pg_insert(AgentPrincipal)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                agent_key=spec.key,
                display_name=spec.name[:120],
                spec_version=spec.version,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "agent_key"])
        )
        row = (
            await db.execute(select(AgentPrincipal).where(AgentPrincipal.agent_key == spec.key))
        ).scalar_one()
    elif row.spec_version != spec.version or row.display_name != spec.name[:120]:
        row.spec_version = spec.version
        row.display_name = spec.name[:120]
    return row


async def owner_principal(db: AsyncSession, tenant_id: uuid.UUID, owner_user_id: uuid.UUID) -> Principal:
    principal = await entity_refs.principal_for_user(db, tenant_id, owner_user_id)
    if principal is None:
        raise OwnerUnavailable(str(owner_user_id))
    return principal


async def portal_principal(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    principal_type: str,
    principal_id: uuid.UUID,
    org_kind: str | None,
    org_id: uuid.UUID | None,
) -> Principal:
    """The live portal principal of a portal-owned run (Epic 66). ``OwnerUnavailable`` when it is missing, not active
    or no longer in ``org_id`` (fail closed)."""
    if principal_type == "client_user" and org_kind == "client":
        cu = (await db.execute(select(ClientUser).where(ClientUser.id == principal_id))).scalar_one_or_none()
        if cu is None or cu.status != "active" or cu.client_id != org_id:
            raise OwnerUnavailable(str(principal_id))
        return Principal(
            type="client_user", id=cu.id, tenant_id=tenant_id, org_id=cu.client_id,
            portal_permissions=frozenset(cu.portal_permissions or ()),
        )  # fmt: skip
    if principal_type == "vendor_user" and org_kind == "vendor":
        vu = (await db.execute(select(VendorUser).where(VendorUser.id == principal_id))).scalar_one_or_none()
        if vu is None or vu.status != "active" or vu.vendor_id != org_id:
            raise OwnerUnavailable(str(principal_id))
        return Principal(
            type="vendor_user", id=vu.id, tenant_id=tenant_id, org_id=vu.vendor_id,
            portal_permissions=frozenset(vu.portal_permissions or ()),
        )  # fmt: skip
    if principal_type == "candidate" and org_kind == "worker" and org_id == principal_id:
        row = (
            await db.execute(select(_candidates.c.erased_at).where(_candidates.c.id == principal_id))
        ).first()
        if row is None or row.erased_at is not None:
            raise OwnerUnavailable(str(principal_id))
        rights = await _candidate_rights[0](db, tenant_id, principal_id) if _candidate_rights else frozenset()
        return Principal(
            type="candidate", id=principal_id, tenant_id=tenant_id, org_id=principal_id, portal_permissions=rights
        )
    raise OwnerUnavailable(str(principal_id))


def portal_rights(portal: Principal, ceiling: Mapping[str, Scope]) -> dict[str, Scope]:
    """AgentSpec ceiling ∩ the portal principal's ``portal_permissions`` (Epic 66, §0.15)."""
    return {k: Scope(v) for k, v in ceiling.items() if k in portal.portal_permissions}


def delegated_rights(owner: Mapping[str, Scope], oauth_scopes: frozenset[str]) -> dict[str, Scope]:
    """§0.5 delegated rights: the live user's permissions restricted to what the token's scopes unlock."""
    from app.core.oauth_scopes import permissions_for

    unlocked = permissions_for(oauth_scopes)
    return {k: Scope(v) for k, v in owner.items() if k in unlocked}


async def effective_principal(
    db: AsyncSession, run: AgentRun, spec: AgentSpec, *, oauth_scopes: frozenset[str] | None = None
) -> Principal:
    if run.owner_principal_type not in (None, "user"):
        portal = await portal_principal(
            db, run.tenant_id, run.owner_principal_type, run.owner_principal_id, run.owner_org_kind, run.owner_org_id
        )
        return Principal(
            type="agent",
            id=run.agent_principal_id,
            tenant_id=run.tenant_id,
            permissions=portal_rights(portal, spec.permission_ceiling),
            org_id=run.owner_org_id,
            portal_permissions=portal.portal_permissions,
        )
    owner = await owner_principal(db, run.tenant_id, run.owner_user_id)
    owner_permissions: Mapping[str, Scope] = owner.permissions
    if run.origin == "external":  # MCP: never more than the delegated token (fail closed without scopes)
        owner_permissions = delegated_rights(owner.permissions, oauth_scopes or frozenset())
    return Principal(
        type="agent",
        id=run.agent_principal_id,
        tenant_id=run.tenant_id,
        permissions=intersect(owner_permissions, spec.permission_ceiling),
        team_ids=owner.team_ids,
        business_unit_id=owner.business_unit_id,
    )


def scope_context(ctx: RequestContext, owner_user_id: uuid.UUID) -> RequestContext:
    """A context for **data-scope checks only** (domain visibility checkers accept user principals): the Effective
    Rights presented as the Accountable Owner (``own`` scope = the owner's records). The actor stays ``agent``
    (``actor_kind``), so ★ guards and audit attribution are unchanged. Never use it for writes."""
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "agent":
        return ctx
    import dataclasses

    scoped = dataclasses.replace(principal, type="user", id=owner_user_id)
    return dataclasses.replace(ctx, principal=scoped)


def tool_context_request(run: AgentRun, principal: Principal, step_no: int) -> RequestContext:
    return RequestContext(
        request_id=f"agent:{run.id}:{step_no}",
        tenant_id=run.tenant_id,
        principal=principal,
        actor_kind="agent",
        actor_id=run.agent_principal_id,
        on_behalf_of=run.owner_user_id,
        agent_run_id=run.id,
        agent_key=run.agent_key,
    )


__all__ = [
    "OwnerUnavailable",
    "delegated_rights",
    "effective_principal",
    "ensure_agent_principal",
    "intersect",
    "narrower",
    "owner_principal",
    "portal_principal",
    "portal_rights",
    "register_candidate_rights",
    "scope_context",
    "tool_context_request",
]
