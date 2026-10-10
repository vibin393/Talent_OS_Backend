"""External principals: ``me`` for the Client Portal, Vendor Portal and the worker "My Work" area (AD-26,
AD-27). ``db`` is the caller's org-scoped session."""

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, Unauthorized
from app.models.external_auth import ClientUser, VendorUser
from app.schemas.external_auth import ExternalMeOut

_candidates = table("candidates", column("id"), column("full_name"))


async def me(ctx: RequestContext, db: AsyncSession) -> ExternalMeOut:
    principal = ctx.principal
    kind = getattr(principal, "type", None)
    if principal is None or ctx.tenant_id is None:
        raise Unauthorized()
    if kind == "client_user":
        row = (await db.execute(select(ClientUser).where(ClientUser.id == principal.id))).scalar_one_or_none()
        if row is None:
            raise NotFound("Not found")
        return ExternalMeOut(
            principal_type="client_user",
            id=row.id,
            tenant_id=row.tenant_id,
            org_kind="client",
            org_id=row.client_id,
            display_name=row.display_name,
            portal_permissions=sorted(row.portal_permissions or ()),
        )
    if kind == "vendor_user":
        vrow = (
            await db.execute(select(VendorUser).where(VendorUser.id == principal.id))
        ).scalar_one_or_none()
        if vrow is None:
            raise NotFound("Not found")
        return ExternalMeOut(
            principal_type="vendor_user",
            id=vrow.id,
            tenant_id=vrow.tenant_id,
            org_kind="vendor",
            org_id=vrow.vendor_id,
            display_name=vrow.display_name,
            portal_permissions=sorted(vrow.portal_permissions or ()),
        )
    if kind == "candidate":
        name = (
            await db.execute(select(_candidates.c.full_name).where(_candidates.c.id == principal.id))
        ).scalar_one_or_none()
        if name is None:
            raise NotFound("Not found")
        return ExternalMeOut(
            principal_type="candidate",
            id=principal.id,
            tenant_id=ctx.tenant_id,
            org_kind="worker",
            org_id=principal.id,
            display_name=str(name),
            portal_permissions=[],
        )
    raise Unauthorized()
