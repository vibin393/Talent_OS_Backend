"""Epic 50 erasure handler ``sso.client_identities`` (FR-68 / AD-34; Epic 35 DSR subject ``client_user``).

When a Client User is erased, their SSO identity links (``client_user_identities``: IdP subject + email at
link) and their break-glass exemption (``client_sso_exemptions``) are deleted. Other subject kinds: no-op.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sso import ClientSsoExemption, ClientUserIdentity
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class ClientSsoIdentitiesHandler:
    name = "sso.client_identities"
    order = 58
    tables: tuple[str, ...] = ("client_user_identities", "client_sso_exemptions")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return {}
        links = await db.execute(
            delete(ClientUserIdentity)
            .where(ClientUserIdentity.client_user_id == ctx.subject_id)
            .returning(ClientUserIdentity.id)
        )
        exemptions = await db.execute(
            delete(ClientSsoExemption)
            .where(ClientSsoExemption.client_user_id == ctx.subject_id)
            .returning(ClientSsoExemption.id)
        )
        return {"client_user_identities": len(links.all()), "client_sso_exemptions": len(exemptions.all())}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return None
        rows = (
            await db.execute(
                select(ClientUserIdentity).where(ClientUserIdentity.client_user_id == ctx.subject_id)
            )
        ).scalars()
        return {
            "sso_identities": [
                {"idp_id": str(r.idp_id), "email_at_link": r.email_at_link, "linked_at": r.linked_at}
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return []
        issues: list[str] = []
        if (
            await db.execute(
                select(ClientUserIdentity.id)
                .where(ClientUserIdentity.client_user_id == ctx.subject_id)
                .limit(1)
            )
        ).first() is not None:
            issues.append("client SSO identity link remains")
        if (
            await db.execute(
                select(ClientSsoExemption.id)
                .where(ClientSsoExemption.client_user_id == ctx.subject_id)
                .limit(1)
            )
        ).first() is not None:
            issues.append("client SSO exemption remains")
        return issues


register_erasure_handler(ClientSsoIdentitiesHandler())
