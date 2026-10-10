# ruff: noqa: E501
"""Test-only OAuth helpers (Epic 66, spec 0.6) for every Phase 5 epic's tests.

* ``make_app(...)`` inserts a platform ``apps`` row (no secret; Epic 67 owns registration).
* ``install_app(tenant_id, app, scopes=..., granted_by=...)`` inserts an active ``app_installations`` row (+ grant).
* ``mint(installation, user=None, scopes=..., resource="api")`` wraps ``oauth_service.issue_token``.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.database import platform_session, tenant_session
from app.models.oauth import App, AppGrant, AppInstallation
from app.schemas.oauth import IssuedTokens
from app.services import oauth_service


async def make_app(
    *,
    name: str = "Test App",
    scopes: Sequence[str] = ("candidates:read",),
    grant_types: Sequence[str] = ("client_credentials", "authorization_code", "refresh_token"),
    resources: Sequence[str] = ("api", "mcp"),
    status: str = "published",
    publisher_kind: str = "platform",
    owner_tenant_id: uuid.UUID | None = None,
) -> App:
    slug = f"test-app-{secrets.token_hex(4)}"
    manifest = {"name": name, "data_flows": []}
    row = App(
        id=uuid.uuid4(),
        client_id=f"tosapp_{secrets.token_hex(13)}",
        name=name,
        slug=slug,
        publisher_kind=publisher_kind,
        owner_tenant_id=owner_tenant_id,
        client_type="confidential",
        grant_types=list(grant_types),
        redirect_uris=["https://example.test/callback"],
        resources=list(resources),
        requested_scopes=list(scopes),
        scope_reasons=dict.fromkeys(scopes, "test"),
        manifest=manifest,
        manifest_sha256=hashlib.sha256(slug.encode()).hexdigest(),
        status=status,
        created_by_kind="platform",
    )
    async with platform_session() as pdb:
        pdb.add(row)
    return row


async def install_app(
    tenant_id: uuid.UUID, app: App, *, scopes: Sequence[str] | None = None, granted_by: uuid.UUID
) -> AppInstallation:
    granted = sorted(scopes if scopes is not None else app.requested_scopes)
    async with tenant_session(tenant_id) as db:
        inst = AppInstallation(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            kind="app",
            app_id=app.id,
            display_name=app.name,
            manifest_version=1,
            manifest_sha256=app.manifest_sha256,
            status="active",
            granted_scopes=granted,
            granted_by=granted_by,
            granted_at=datetime.now(UTC),
        )
        db.add(inst)
        await db.flush()
        db.add(
            AppGrant(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                installation_id=inst.id,
                manifest_version=1,
                manifest_sha256=app.manifest_sha256,
                scopes=granted,
                data_flows=[],
                granted_by=granted_by,
                granted_at=datetime.now(UTC),
            )
        )
    return inst


async def mint(
    installation: AppInstallation,
    user: uuid.UUID | None = None,
    scopes: Sequence[str] = ("candidates:read",),
    resource: str = "api",
    *,
    with_refresh: bool = False,
) -> IssuedTokens:
    async with tenant_session(installation.tenant_id) as db:
        auth_id = None
        if user is not None:
            auth_id = await oauth_service.record_user_authorization(
                db, installation_id=installation.id, user_id=user, scopes=list(scopes), resource=resource
            )
        return await oauth_service.issue_token(
            db,
            installation_id=installation.id,
            grant_type="client_credentials" if user is None else "authorization_code",
            resource=resource,  # type: ignore[arg-type]
            scopes=list(scopes),
            user_id=user,
            user_authorization_id=auth_id,
            with_refresh=with_refresh,
        )
