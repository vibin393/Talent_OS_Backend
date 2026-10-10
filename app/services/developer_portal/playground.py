# ruff: noqa: E501
"""The playground token calls (Epic 74, stories 74.3 / 74.4, spec 0.5). The pasted client secret is used once,
in-process, against Epic 67's token endpoint function (``oauth_service.token`` -- the same code path as
``POST /api/oauth/token``, no network), and discarded: it is never stored, cached or logged. Only Sandbox Tenants are
accepted (409 ``sandbox_only``)."""

from app.core.database import platform_session
from app.core.errors import ValidationFailed
from app.models.identity import Tenant
from app.schemas.developer import PlaygroundExchangeIn, PlaygroundTokenIn, PlaygroundTokenOut
from app.schemas.oauth import ClientAuth, TokenRequest
from app.services import oauth_service
from app.services.developer_portal import audit_log
from app.services.developer_portal.owner import Owner
from app.services.developer_portal.sandboxes import get_owned, owner_app


async def _slug(tenant_id: object) -> str:
    async with platform_session() as db:
        t = await db.get(Tenant, tenant_id)
        assert t is not None
        return str(t.slug)


def _oauth_failed(exc: oauth_service.OAuthError) -> ValidationFailed:
    body = exc.body()
    return ValidationFailed(str(body.get("error_description") or "The token request was refused"), code=str(body.get("error") or "invalid_request"))


async def playground_token(owner: Owner, data: PlaygroundTokenIn) -> PlaygroundTokenOut:
    owner.require("developer")
    row = await get_owned(owner, data.sandbox_id, require_ready=True)
    app = await owner_app(owner, data.app_id)
    slug = await _slug(row.tenant_id)
    try:
        out = await oauth_service.token(
            TokenRequest(grant_type="client_credentials", tenant=slug, scope=data.scope or None, resource="api"),
            ClientAuth(client_id=app.client_id, client_secret=data.client_secret.get_secret_value()),
        )
    except oauth_service.OAuthError as exc:
        raise _oauth_failed(exc) from None
    if owner.kind == "partner_org":
        await audit_log.record("playground.token_issued", partner_org_id=owner.id, account_id=owner.account_id, target_type="app", target_id=app.id, grant="client_credentials", sandbox_id=row.id)
    return PlaygroundTokenOut(access_token=out.access_token, expires_in=out.expires_in, scope=out.scope, refresh_token=out.refresh_token, tenant_slug=slug)


async def playground_exchange(owner: Owner, data: PlaygroundExchangeIn) -> PlaygroundTokenOut:
    owner.require("developer")
    row = await get_owned(owner, data.sandbox_id, require_ready=True)
    app = await owner_app(owner, data.app_id)
    slug = await _slug(row.tenant_id)
    secret = data.client_secret.get_secret_value() if data.client_secret is not None else None
    try:
        out = await oauth_service.token(
            TokenRequest(grant_type="authorization_code", code=data.code, redirect_uri=data.redirect_uri, code_verifier=data.code_verifier),
            ClientAuth(client_id=app.client_id, client_secret=secret),
        )
    except oauth_service.OAuthError as exc:
        raise _oauth_failed(exc) from None
    if owner.kind == "partner_org":
        await audit_log.record("playground.token_issued", partner_org_id=owner.id, account_id=owner.account_id, target_type="app", target_id=app.id, grant="authorization_code", sandbox_id=row.id)
    return PlaygroundTokenOut(access_token=out.access_token, expires_in=out.expires_in, scope=out.scope, refresh_token=out.refresh_token, tenant_slug=slug)


__all__ = ["playground_exchange", "playground_token"]
