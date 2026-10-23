# ruff: file-ignore[line-too-long]
"""The owner of workspace resources (spec A3 "dctx_or_ctx"): a partner org selected by a ``DeveloperContext``, or a
Tenant whose human user holds ``app:develop`` (Tenant Developers, normal session). Partners see only their org's
sandboxes, Apps and runs; Tenant Developers only their Tenant's private Apps and ``owner_kind='tenant'`` sandboxes."""

import dataclasses
import uuid
from typing import Literal

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.developer import DeveloperContext
from app.core.errors import Forbidden, NotFound
from app.core.permissions import Principal, check
from app.models.developer import DeveloperAccount
from app.models.identity import User
from app.services.developer_portal.common import ROLE_RANK, require_org


@dataclasses.dataclass(frozen=True, slots=True)
class Owner:
    kind: Literal["partner_org", "tenant"]
    id: uuid.UUID  # partner org id | tenant id
    role: str  # owner | developer | viewer (Tenant Developers act as "developer")
    email: str
    name: str
    account_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    dctx: DeveloperContext | None = None
    ctx: RequestContext | None = None

    @property
    def actor_id(self) -> uuid.UUID:
        return self.account_id or self.user_id or self.id

    def require(self, minimum: str) -> None:
        if ROLE_RANK.get(self.role, -1) < ROLE_RANK[minimum]:
            raise Forbidden("Your role in this organisation does not allow this", code="org_role_required")

    @property
    def partner_org_id(self) -> uuid.UUID | None:
        return self.id if self.kind == "partner_org" else None

    @property
    def owner_tenant_id(self) -> uuid.UUID | None:
        return self.id if self.kind == "tenant" else None


async def partner(dctx: DeveloperContext) -> Owner:
    org = require_org(dctx)
    async with platform_session() as db:
        account = await db.get(DeveloperAccount, dctx.developer_id)
        if account is None:
            raise NotFound("Account not found")
        return Owner(kind="partner_org", id=org, role=str(dctx.role or "viewer"), email=str(account.email), name=account.name,
                     account_id=account.id, dctx=dctx)  # fmt: skip


async def tenant(ctx: RequestContext) -> Owner:
    require_human(actor_from_ctx(ctx))
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or ctx.tenant_id is None:
        raise Forbidden("Sign in to your workspace", code="forbidden")
    check(principal, "app:develop")
    async with tenant_session(ctx.tenant_id) as db:
        user = await db.get(User, principal.id)
        if user is None:
            raise NotFound("User not found")
        return Owner(kind="tenant", id=ctx.tenant_id, role="developer", email=str(user.email), name=user.name, user_id=user.id, ctx=ctx)


__all__ = ["Owner", "partner", "tenant"]
