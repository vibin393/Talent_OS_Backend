# ruff: noqa: E501
"""Epic 66 (stories 66.2 / 66.3, spec 0.5 / 0.6): the OAuth token store, App Principals and Effective Rights.

``issue_token`` invariants (D-7, resource mcp => user, scopes within the Grant); authentication failures within the
cache window (suspended app, uninstalled installation, deactivated user, kill switch, wrong resource); ``tos_at_`` on
the internal API -> 403 ``app_token_not_allowed``; ``revoke_for``."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.actors import AUTOMATED_KINDS, actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError, Forbidden
from app.core.permissions import Principal
from app.models.identity import User
from app.models.oauth import App, AppInstallation, OAuthToken
from app.services import ecosystem_service, oauth_service
from tests.epic3 import Org3, create_candidate
from tests.modules.identity.conftest import new_client
from tests.support.oauth import install_app, make_app, mint

META = "/api/public/v1/meta"
SCOPES = ("candidates:read", "pii.contact:read", "mcp:read")


async def _setup(org: Org3, scopes: tuple[str, ...] = SCOPES) -> AppInstallation:
    app = await make_app(scopes=scopes)
    return await install_app(org.tenant_id, app, granted_by=org.admin.user_id)


async def _call(app: FastAPI, token: str, path: str = META) -> tuple[int, dict[str, Any]]:
    async with new_client(app) as client:
        resp = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    return resp.status_code, (resp.json() if resp.content else {})


def _code(body: dict[str, Any]) -> str | None:
    return (body.get("error") or {}).get("code")


def _fresh() -> None:
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()


async def test_client_credentials_never_carry_sensitive_scopes(org3: Org3) -> None:
    inst = await _setup(org3)
    with pytest.raises(DomainError) as exc:
        await mint(inst, scopes=("candidates:read", "pii.contact:read"))
    assert exc.value.code == "invalid_scope"
    issued = await mint(inst, scopes=("candidates:read",))
    assert issued.access_token.startswith("tos_at_") and issued.refresh_token is None
    async with tenant_session(org3.tenant_id) as db:
        row = (await db.execute(select(OAuthToken))).scalar_one()
    assert issued.access_token.encode() not in row.token_hash and len(row.token_hash) == 32
    assert "tos_at_" not in repr(issued)


async def test_scopes_within_grant_and_mcp_needs_user(org3: Org3) -> None:
    inst = await _setup(org3, scopes=("candidates:read", "mcp:read"))
    with pytest.raises(DomainError) as exc:
        await mint(inst, scopes=("applications:read",))
    assert exc.value.code == "invalid_scope"
    with pytest.raises(DomainError):
        await mint(inst, scopes=("mcp:read",), resource="mcp")
    issued = await mint(inst, user=org3.admin.user_id, scopes=("mcp:read", "candidates:read"), resource="mcp")
    assert sorted(issued.scopes) == ["candidates:read", "mcp:read"]  # D-25(a): base scopes ride along


async def test_app_principal_on_public_api_and_refused_internally(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (await mint(inst, scopes=("candidates:read",))).access_token
    status, body = await _call(app, token)
    assert status == 200, body
    assert body["principal"] == {
        "type": "app",
        "app_id": str(inst.app_id),
        "installation_id": str(inst.id),
        "user_id": None,
    }
    assert body["scopes"] == ["candidates:read"]
    status, body = await _call(app, token, "/api/v1/candidates")
    assert (status, _code(body)) == (403, "app_token_not_allowed")


async def test_wrong_resource_is_401(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (await mint(inst, user=org3.admin.user_id, scopes=("mcp:read",), resource="mcp")).access_token
    status, body = await _call(app, token)
    assert (status, _code(body)) == (401, "invalid_token")
    assert body["error"]["details"]["error_description"] == "wrong resource"


async def test_delegated_token_follows_the_live_user(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (
        await mint(inst, user=org3.admin.user_id, scopes=("candidates:read", "pii.contact:read"))
    ).access_token
    status, body = await _call(app, token)
    assert status == 200 and body["principal"]["user_id"] == str(org3.admin.user_id)
    assert body["scopes"] == ["candidates:read", "pii.contact:read"]
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(User).where(User.id == org3.admin.user_id).values(is_active=False))
    _fresh()  # the 60 s cache window, collapsed
    status, body = await _call(app, token)
    assert (status, _code(body)) == (401, "invalid_token")


async def test_revoke_for_fails_the_next_call(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (await mint(inst, scopes=("candidates:read",))).access_token
    assert (await _call(app, token))[0] == 200
    async with tenant_session(org3.tenant_id) as db:
        assert await oauth_service.revoke_for(db, installation_id=inst.id, reason="revoked") == 1
    status, body = await _call(app, token)
    assert (status, _code(body)) == (401, "invalid_token")


async def test_suspended_app_and_uninstalled_installation(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (await mint(inst, scopes=("candidates:read",))).access_token
    assert (await _call(app, token))[0] == 200
    async with platform_session() as pdb:
        await pdb.execute(update(App).where(App.id == inst.app_id).values(status="suspended"))
    _fresh()
    status, body = await _call(app, token)
    assert (status, _code(body)) == (401, "invalid_token")
    async with platform_session() as pdb:
        await pdb.execute(update(App).where(App.id == inst.app_id).values(status="published"))
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(
            update(AppInstallation).where(AppInstallation.id == inst.id).values(status="uninstalled")
        )
    _fresh()
    status, body = await _call(app, token)
    assert (status, _code(body)) == (401, "invalid_token")


async def test_kill_switch_suspends_external_access_only(app: FastAPI, org3: Org3) -> None:
    inst = await _setup(org3)
    token = (await mint(inst, scopes=("candidates:read",))).access_token
    await create_candidate(org3.admin)
    human = RequestContext(
        tenant_id=org3.tenant_id,
        principal=Principal(type="user", id=org3.admin.user_id, tenant_id=org3.tenant_id),
    )
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=True, reason="incident")
    status, body = await _call(app, token)
    assert (status, _code(body)) == (403, "ecosystem_suspended")
    assert (await org3.admin.client.get("/api/v1/candidates")).status_code == 200  # the web app is unaffected
    app_ctx = RequestContext(
        tenant_id=org3.tenant_id,
        principal=Principal(type="app", id=inst.id, tenant_id=org3.tenant_id, app_installation_id=inst.id),
    )
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await ecosystem_service.set_kill_switch(app_ctx, db, on=False, reason="nope")
    assert exc.value.code == "human_only_action"  # lifting is ★
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=False, reason="resolved")
    assert (await _call(app, token))[0] == 200


def test_app_actor_is_automated_and_never_human() -> None:
    inst_id, user_id = uuid.uuid4(), uuid.uuid4()
    ctx = RequestContext(
        tenant_id=uuid.uuid4(),
        principal=Principal(type="app", id=inst_id, app_installation_id=inst_id, acting_user_id=user_id),
    )
    actor = actor_from_ctx(ctx)
    assert (actor.kind, actor.id, actor.on_behalf_of) == ("app", inst_id, user_id)
    assert "app" in AUTOMATED_KINDS
    with pytest.raises(Forbidden) as exc:
        require_human(actor)
    assert exc.value.code == "human_only_action"
