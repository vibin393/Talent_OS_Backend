# ruff: file-ignore[line-too-long]
"""Partner App credentials (Epic 74, story 74.4): thin wrappers over Epic 67's ``oauth_service`` adding the org role
check, MFA (the router requires it) and the org audit log. Calls run from a platform context (``tenant_id`` None) with
``Publisher(kind="partner")``; Apps start in ``development``; secrets are shown once."""

import uuid

from sqlalchemy import select

from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import NotFound
from app.models.oauth import App, AppSecret
from app.schemas.oauth import AppCreate, AppCreated, AppOut, AppUpdate, Publisher, SecretCreated
from app.services import oauth_service
from app.services.developer_portal import audit_log
from app.services.developer_portal.common import platform_ctx, require_role
from app.services.oauth.apps import app_out


async def _org_app(dctx: DeveloperContext, app_id: uuid.UUID, minimum: str) -> App:
    org = require_role(dctx, minimum)
    async with platform_session() as db:
        app = await db.get(App, app_id)
    if app is None or app.publisher_kind != "partner" or app.partner_org_id != org:
        raise NotFound("App not found")
    return app


async def list_org_apps(dctx: DeveloperContext, org_id: uuid.UUID) -> list[AppOut]:
    require_role(dctx, "viewer", org_id)
    async with platform_session() as db:
        rows = (await db.execute(select(App).where(App.publisher_kind == "partner", App.partner_org_id == org_id).order_by(App.created_at.desc()))).scalars()
        return [app_out(r) for r in rows]


async def get_org_app(dctx: DeveloperContext, app_id: uuid.UUID) -> AppOut:
    return app_out(await _org_app(dctx, app_id, "viewer"))


async def list_org_app_secrets(dctx: DeveloperContext, app_id: uuid.UUID) -> list[dict[str, object]]:
    await _org_app(dctx, app_id, "viewer")
    async with platform_session() as db:
        rows = (await db.execute(select(AppSecret).where(AppSecret.app_id == app_id).order_by(AppSecret.created_at.desc()))).scalars()
        return [
            {"id": r.id, "prefix": r.prefix, "fingerprint": r.fingerprint, "created_at": r.created_at, "expires_at": r.expires_at,
             "last_used_at": r.last_used_at, "revoked_at": r.revoked_at}
            for r in rows
        ]  # fmt: skip


async def create_org_app(dctx: DeveloperContext, org_id: uuid.UUID, data: AppCreate) -> AppCreated:
    require_role(dctx, "developer", org_id)
    ctx = platform_ctx(dctx.developer_id)
    async with platform_session() as db:
        out = await oauth_service.create_app(ctx, db, data, publisher=Publisher(kind="partner", partner_org_id=org_id, created_by_kind="developer", created_by_id=dctx.developer_id))
        audit_log.add(db, "app.created", partner_org_id=org_id, account_id=dctx.developer_id, target_type="app", target_id=out.id, name=out.name)
    return out


async def update_org_app(dctx: DeveloperContext, app_id: uuid.UUID, data: AppUpdate) -> AppOut:
    app = await _org_app(dctx, app_id, "developer")
    async with platform_session() as db:
        out = await oauth_service.update_app(platform_ctx(dctx.developer_id), db, app_id, data)
        audit_log.add(db, "app.updated", partner_org_id=app.partner_org_id, account_id=dctx.developer_id, target_type="app", target_id=app_id, fields=sorted(data.model_fields_set))
    return out


async def rotate_org_app_secret(dctx: DeveloperContext, app_id: uuid.UUID) -> SecretCreated:
    app = await _org_app(dctx, app_id, "developer")
    async with platform_session() as db:
        out = await oauth_service.rotate_secret(platform_ctx(dctx.developer_id), db, app_id)
        audit_log.add(db, "app.secret_rotated", partner_org_id=app.partner_org_id, account_id=dctx.developer_id, target_type="app", target_id=app_id, app_name=app.name, fingerprint=out.fingerprint)
    return out


async def revoke_org_app_secret(dctx: DeveloperContext, app_id: uuid.UUID, secret_id: uuid.UUID, *, confirm_last: bool = False) -> None:
    app = await _org_app(dctx, app_id, "developer")
    async with platform_session() as db:
        await oauth_service.revoke_secret(platform_ctx(dctx.developer_id), db, app_id, secret_id, confirm_last=confirm_last)
        audit_log.add(db, "app.secret_revoked", partner_org_id=app.partner_org_id, account_id=dctx.developer_id, target_type="app", target_id=app_id, app_name=app.name)


async def retire_org_app(dctx: DeveloperContext, app_id: uuid.UUID, reason: str) -> AppOut:
    """67's ``retire_app`` covers tenant Apps only; partner Apps go through ``set_app_status`` (same runtime effect)."""
    app = await _org_app(dctx, app_id, "developer")
    async with platform_session() as db:
        out = await oauth_service.set_app_status(platform_ctx(dctx.developer_id), db, app_id, "retired", reason[:500])
        audit_log.add(db, "app.retired", partner_org_id=app.partner_org_id, account_id=dctx.developer_id, target_type="app", target_id=app_id, app_name=app.name)
    return out


__all__ = [
    "create_org_app",
    "get_org_app",
    "list_org_app_secrets",
    "list_org_apps",
    "retire_org_app",
    "revoke_org_app_secret",
    "rotate_org_app_secret",
    "update_org_app",
]
