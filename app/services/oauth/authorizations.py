"""Delegated authorisations (Epic 66 store; Epic 67 UI): one live row per (installation, user, resource)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import NotFound
from app.core.events import emit
from app.events.ecosystem import UserGrantChanged
from app.models.oauth import App, AppInstallation, AppUserAuthorization
from app.schemas.oauth import UserAuthorizationOut
from app.services.oauth.tokens import revoke_for


def _now() -> datetime:
    return datetime.now(UTC)


async def record_user_authorization(
    db: AsyncSession, *, installation_id: uuid.UUID, user_id: uuid.UUID, scopes: Sequence[str], resource: str
) -> uuid.UUID:
    """Upsert the live authorisation (a new consent replaces the previous scopes)."""
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    row = (
        await db.execute(
            select(AppUserAuthorization)
            .where(
                AppUserAuthorization.installation_id == installation_id,
                AppUserAuthorization.user_id == user_id,
                AppUserAuthorization.resource == resource,
                AppUserAuthorization.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = AppUserAuthorization(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            installation_id=installation_id,
            user_id=user_id,
            scopes=sorted(set(scopes)),
            resource=resource,
            granted_at=_now(),
        )
        db.add(row)
    else:
        row.scopes = sorted(set(scopes))
        row.granted_at = _now()
    await db.flush()
    emit(
        db,
        UserGrantChanged(
            tenant_id=tenant_id,
            installation_id=installation_id,
            user_id=user_id,
            resource=resource,
            change="granted",
        ),
    )
    return row.id


async def list_my_authorizations(ctx: RequestContext, db: AsyncSession) -> list[UserAuthorizationOut]:
    user_id = getattr(ctx.principal, "id", None)
    rows = list(
        (
            await db.execute(
                select(AppUserAuthorization, AppInstallation)
                .join(AppInstallation, AppInstallation.id == AppUserAuthorization.installation_id)
                .where(AppUserAuthorization.user_id == user_id, AppUserAuthorization.revoked_at.is_(None))
                .order_by(AppUserAuthorization.granted_at.desc())
            )
        ).all()
    )
    app_ids = {inst.app_id for _, inst in rows if inst.app_id}
    names: dict[uuid.UUID, str] = {}
    if app_ids:
        async with platform_session() as pdb:
            names = {
                a.id: a.name for a in (await pdb.execute(select(App).where(App.id.in_(app_ids)))).scalars()
            }
    return [
        UserAuthorizationOut(
            id=auth.id,
            installation_id=inst.id,
            app_id=inst.app_id,
            app_name=names.get(inst.app_id, inst.display_name) if inst.app_id else inst.display_name,
            resource=auth.resource,  # type: ignore[arg-type]
            scopes=list(auth.scopes),
            granted_at=auth.granted_at,
            revoked_at=auth.revoked_at,
        )
        for auth, inst in rows
    ]


async def revoke_my_authorization(ctx: RequestContext, db: AsyncSession, authorization_id: uuid.UUID) -> None:
    user_id = getattr(ctx.principal, "id", None)
    row = (
        await db.execute(
            select(AppUserAuthorization)
            .where(AppUserAuthorization.id == authorization_id, AppUserAuthorization.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Authorization not found")
    if row.revoked_at is None:
        row.revoked_at, row.revoked_reason = _now(), "revoked"
        await db.flush()
        await revoke_for(db, installation_id=row.installation_id, user_id=row.user_id, reason="revoked")
        audit.record("oauth.authorization_revoked", "app_user_authorization", row.id, session=db)
        emit(
            db,
            UserGrantChanged(
                tenant_id=row.tenant_id,
                installation_id=row.installation_id,
                user_id=row.user_id,
                resource=row.resource,
                change="revoked",
            ),
        )


__all__ = ["list_my_authorizations", "record_user_authorization", "revoke_my_authorization"]
