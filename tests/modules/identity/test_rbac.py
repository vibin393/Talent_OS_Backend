"""Story 1.5: default roles, custom roles, permission catalog, teams, business units, scope_filter and
cross-tenant isolation of every admin endpoint."""

import dataclasses
import re
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer, get_mailer
from app.core.permissions import (
    ALL_PERMISSIONS,
    CATALOG,
    DEFAULT_ROLE_KEYS,
    PermissionDef,
    Scope,
    scope_filter,
)
from app.core.settings import get_settings
from app.models.identity import Role, User
from app.services import access_service as access

from .conftest import USER_PASSWORD, Org, csrf, join, make_tenant, new_client, tenant_body

ROLES = "/api/v1/admin/roles"
TEAMS = "/api/v1/admin/teams"
BUS = "/api/v1/admin/business-units"
USERS = "/api/v1/admin/users"
ME = "/api/v1/auth/me"
INVITE_RE = re.compile(re.escape(get_settings().frontend_url.rstrip("/")) + r"/accept-invite#token=(\S+)")


async def _role_id(tenant_id: uuid.UUID, key: str) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(Role.id).where(Role.key == key))).scalar_one()


# ---------------------------------------------------------------- acceptance criterion
async def test_provisioned_admin_holds_tenant_admin_with_every_permission(
    op_client: httpx.AsyncClient, app: FastAPI
) -> None:
    created = await op_client.post("/api/v1/platform/tenants", json=tenant_body(), headers=csrf(op_client))
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])

    async with tenant_session(tenant_id) as db:
        keys = set((await db.execute(select(Role.key).where(Role.is_system.is_(True)))).scalars())
    assert keys == DEFAULT_ROLE_KEYS

    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    (mail,) = [m for m in mailer.sent if m.tenant_id == tenant_id]
    token = INVITE_RE.search(mail.text).group(1)  # type: ignore[union-attr]
    async with new_client(app) as client:
        accepted = await client.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"name": "Ada", "password": USER_PASSWORD}
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["user"]["role_keys"] == ["tenant_admin"]
        me = await client.get(ME)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["role_keys"] == ["tenant_admin"]
    assert me.json()["permissions"] == dict.fromkeys(sorted(ALL_PERMISSIONS), "tenant")


# ---------------------------------------------------------------- authorization
async def test_missing_permission_is_403_with_permission_detail(app: FastAPI, org: Org) -> None:
    recruiter = await join(app, org.tenant, "recruiter")
    try:
        me = (await recruiter.client.get(ME)).json()
        assert me["user"]["role_keys"] == ["recruiter"]
        assert me["permissions"]["candidate:read"] == "tenant"
        assert "user:read" not in me["permissions"]
        for method, path, perm in [
            ("GET", USERS, "user:read"),
            ("GET", ROLES, "role:read"),
            ("GET", "/api/v1/permissions", "role:read"),
            ("POST", TEAMS, "team:manage"),
            ("POST", BUS, "business_unit:manage"),
            ("POST", f"{USERS}/{recruiter.user_id}/deactivate", "user:manage"),
        ]:
            resp = await recruiter.client.request(method, path, json={"name": "x"}, headers=recruiter.h())
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["error"]["code"] == "forbidden"
            assert resp.json()["error"]["details"]["permission"] == perm
        # team:read is granted to recruiters (directory read)
        assert (await recruiter.client.get(TEAMS)).status_code == 200
    finally:
        await recruiter.client.aclose()


async def test_admin_endpoints_require_authentication(client: httpx.AsyncClient) -> None:
    for path in (USERS, ROLES, TEAMS, BUS, "/api/v1/permissions", "/api/v1/admin/invitations"):
        assert (await client.get(path)).status_code == 401


async def test_role_changes_apply_to_existing_sessions(app: FastAPI, org: Org) -> None:
    viewer = await join(app, org.tenant, "viewer")
    try:
        assert (await viewer.client.get(USERS)).status_code == 403
        admin_role = await _role_id(org.tenant.id, "tenant_admin")
        resp = await org.admin.client.patch(
            f"{USERS}/{viewer.user_id}", json={"role_ids": [str(admin_role)]}, headers=org.admin.h()
        )
        assert resp.status_code == 200, resp.text
        assert (await viewer.client.get(USERS)).status_code == 200
    finally:
        await viewer.client.aclose()


# ---------------------------------------------------------------- permissions catalog
async def test_permission_catalog(org: Org) -> None:
    resp = await org.admin.client.get("/api/v1/permissions")
    assert resp.status_code == 200
    catalog = {p["key"]: p for p in resp.json()}
    assert set(catalog) == set(CATALOG)
    assert catalog["candidate:read"]["scopes"] == ["own", "team", "bu", "tenant"]
    assert catalog["user:manage"]["scopes"] == ["tenant"]
    assert catalog["compensation:view"]["group"]


# ---------------------------------------------------------------- roles
async def test_role_crud_and_rules(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    listed = (await c.get(ROLES)).json()
    assert {r["key"] for r in listed} == DEFAULT_ROLE_KEYS
    admin_role = next(r for r in listed if r["key"] == "tenant_admin")
    assert admin_role["is_system"] and admin_role["is_immutable"] and admin_role["user_count"] == 1

    body = {"key": "Sourcer", "name": "Sourcer", "permissions": {"candidate:read": "team"}}
    created = await c.post(ROLES, json=body, headers=h)
    assert created.status_code == 201, created.text
    role = created.json()
    assert (role["key"], role["is_system"], role["permissions"]) == (
        "sourcer",
        False,
        {"candidate:read": "team"},
    )
    assert (await c.post(ROLES, json=body, headers=h)).json()["error"]["code"] == "role_exists"

    bad_perm = await c.post(
        ROLES, json={**body, "key": "x1", "permissions": {"candidate:fly": "own"}}, headers=h
    )
    assert bad_perm.status_code == 422
    assert bad_perm.json()["error"]["code"] == "invalid_permission"
    assert bad_perm.json()["error"]["details"]["permission"] == "candidate:fly"
    bad_scope = await c.post(
        ROLES, json={**body, "key": "x2", "permissions": {"candidate:read": "planet"}}, headers=h
    )
    assert bad_scope.status_code == 422
    admin_only = await c.post(
        ROLES, json={**body, "key": "x3", "permissions": {"user:manage": "own"}}, headers=h
    )
    assert admin_only.status_code == 422
    bad_key = await c.post(ROLES, json={**body, "key": "9 lives"}, headers=h)
    assert bad_key.status_code == 422

    updated = await c.patch(
        f"{ROLES}/{role['id']}",
        json={"key": "talent_sourcer", "permissions": {"candidate:read": "bu", "task:read": "own"}},
        headers=h,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["key"] == "talent_sourcer"
    assert updated.json()["permissions"] == {"candidate:read": "bu", "task:read": "own"}
    assert (await c.get(f"{ROLES}/{role['id']}")).json()["key"] == "talent_sourcer"

    # role in use cannot be deleted
    member = await c.post(
        f"{USERS}/invite", json={"email": "x@example.com", "role_ids": [role["id"]]}, headers=h
    )
    assert member.status_code == 201
    deleted = await c.delete(f"{ROLES}/{role['id']}", headers=h)
    assert deleted.status_code == 204  # invitations do not pin a role
    assert (await c.get(f"{ROLES}/{role['id']}")).status_code == 404


async def test_role_in_use_and_system_roles(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    created = (await c.post(ROLES, json={"key": "custom", "name": "Custom"}, headers=h)).json()
    other = await join(app, org.tenant, "custom")
    await other.client.aclose()
    in_use = await c.delete(f"{ROLES}/{created['id']}", headers=h)
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "role_in_use"

    recruiter = await _role_id(org.tenant.id, "recruiter")
    admin = await _role_id(org.tenant.id, "tenant_admin")
    for role_id in (recruiter, admin):
        resp = await c.delete(f"{ROLES}/{role_id}", headers=h)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "system_role"
    rekey = await c.patch(f"{ROLES}/{recruiter}", json={"key": "hunter"}, headers=h)
    assert rekey.json()["error"]["code"] == "system_role"
    # system role permissions are editable, except Tenant Admin's
    edited = await c.patch(f"{ROLES}/{recruiter}", json={"permissions": {"candidate:read": "own"}}, headers=h)
    assert edited.status_code == 200
    assert edited.json()["permissions"] == {"candidate:read": "own"}
    frozen = await c.patch(f"{ROLES}/{admin}", json={"permissions": {"candidate:read": "own"}}, headers=h)
    assert frozen.status_code == 409
    assert frozen.json()["error"]["code"] == "system_role"
    renamed = await c.patch(f"{ROLES}/{admin}", json={"name": "Owner", "description": "Top"}, headers=h)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Owner"


# ---------------------------------------------------------------- business units & teams
async def test_business_units_and_teams(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    joined = await join(app, org.tenant, "viewer")
    await joined.client.aclose()
    member_id = joined.user_id
    bu = await c.post(BUS, json={"name": "North", "description": "N"}, headers=h)
    assert bu.status_code == 201, bu.text
    bu_id = bu.json()["id"]
    assert (await c.post(BUS, json={"name": "north"}, headers=h)).json()["error"]["code"] == (
        "business_unit_exists"
    )
    assert (await c.patch(f"{BUS}/{bu_id}", json={"name": "North East"}, headers=h)).json()["name"] == (
        "North East"
    )
    assert (await c.get(BUS)).json()["total"] == 1

    team = await c.post(TEAMS, json={"name": "Java Squad", "business_unit_id": bu_id}, headers=h)
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    assert team.json()["members"] == []
    assert (await c.post(TEAMS, json={"name": "java squad"}, headers=h)).json()["error"]["code"] == (
        "team_exists"
    )
    bad_bu = await c.post(TEAMS, json={"name": "T2", "business_unit_id": str(uuid.uuid4())}, headers=h)
    assert bad_bu.status_code == 422
    assert bad_bu.json()["error"]["code"] == "invalid_reference"

    member = await c.put(f"{TEAMS}/{team_id}/members/{member_id}", json={"is_lead": True}, headers=h)
    assert member.status_code == 200, member.text
    assert member.json()["members"][0]["is_lead"] is True
    assert member.json()["member_count"] == 1
    again = await c.put(f"{TEAMS}/{team_id}/members/{member_id}", json={"is_lead": False}, headers=h)
    assert again.json()["members"][0]["is_lead"] is False
    unknown_user = await c.put(f"{TEAMS}/{team_id}/members/{uuid.uuid4()}", json={}, headers=h)
    assert unknown_user.status_code == 404

    listed = (await c.get(TEAMS, params={"business_unit_id": bu_id})).json()
    assert [t["member_count"] for t in listed["items"]] == [1]
    assert (await c.get(USERS, params={"team_id": team_id})).json()["total"] == 1

    in_use = await c.delete(f"{BUS}/{bu_id}", headers=h)
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "business_unit_in_use"

    assert (await c.delete(f"{TEAMS}/{team_id}/members/{member_id}", headers=h)).status_code == 204
    assert (await c.delete(f"{TEAMS}/{team_id}/members/{member_id}", headers=h)).status_code == 404
    moved = await c.patch(f"{TEAMS}/{team_id}", json={"business_unit_id": None, "name": "Kotlin"}, headers=h)
    assert moved.json()["business_unit_id"] is None
    assert moved.json()["name"] == "Kotlin"
    assert (await c.delete(f"{BUS}/{bu_id}", headers=h)).status_code == 204
    assert (await c.delete(f"{TEAMS}/{team_id}", headers=h)).status_code == 204
    assert (await c.get(f"{TEAMS}/{team_id}")).status_code == 404


# ---------------------------------------------------------------- scope_filter against real rows
async def test_scope_filter_team_bu_own_tenant(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    scoped = await c.post(
        ROLES,
        json={"key": "team_reader", "name": "Team reader", "permissions": {"candidate:read": "team"}},
        headers=h,
    )
    assert scoped.status_code == 201
    me = await join(app, org.tenant, "team_reader", name="Me")
    mate = await join(app, org.tenant, "viewer", name="Mate")
    same_bu = await join(app, org.tenant, "viewer", name="Same BU")
    stranger = await join(app, org.tenant, "viewer", name="Stranger")
    for m in (me, mate, same_bu, stranger):
        await m.client.aclose()
    bu_id = (await c.post(BUS, json={"name": "South"}, headers=h)).json()["id"]
    team_id = (await c.post(TEAMS, json={"name": "Alpha"}, headers=h)).json()["id"]
    for m in (me, mate):
        assert (await c.put(f"{TEAMS}/{team_id}/members/{m.user_id}", json={}, headers=h)).status_code == 200
    for m in (me, same_bu):
        resp = await c.patch(f"{USERS}/{m.user_id}", json={"business_unit_id": bu_id}, headers=h)
        assert resp.status_code == 200

    async def visible(permissions: dict[str, Scope], *, bu_col: bool = True) -> set[uuid.UUID]:
        async with tenant_session(org.tenant.id) as db:
            user = await db.get(User, me.user_id)
            assert user is not None
            base = access.user_principal(
                user_id=user.id,
                tenant_id=org.tenant.id,
                session_id=None,
                business_unit_id=user.business_unit_id,
                access=await access.load_access(db, user.id),
            )
            principal = dataclasses.replace(base, permissions=permissions) if permissions else base
            bu = User.business_unit_id if bu_col else None
            stmt = scope_filter(select(User.id), principal, "candidate:read", User.id, bu)
            return set((await db.execute(stmt)).scalars())

    everyone = {org.admin.user_id, me.user_id, mate.user_id, same_bu.user_id, stranger.user_id}
    assert await visible({}) == {me.user_id, mate.user_id}  # the real role: team scope
    assert await visible({"candidate:read": Scope.OWN}) == {me.user_id}
    assert await visible({"candidate:read": Scope.BU}) == {me.user_id, mate.user_id, same_bu.user_id}
    # no BU column on the rows: falls back to "owned by a user of my BU" (plus team rows)
    bu_fallback = await visible({"candidate:read": Scope.BU}, bu_col=False)
    assert bu_fallback == {me.user_id, mate.user_id, same_bu.user_id}
    assert await visible({"candidate:read": Scope.TENANT}) == everyone
    assert await visible({"task:read": Scope.TENANT}) == set()


# ---------------------------------------------------------------- cross-tenant isolation
async def test_cross_tenant_ids_are_404(app: FastAPI, org: Org, other_org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    o, oh = other_org.admin.client, other_org.admin.h()
    their_role = (await o.post(ROLES, json={"key": "theirs", "name": "Theirs"}, headers=oh)).json()["id"]
    their_team = (await o.post(TEAMS, json={"name": "Theirs"}, headers=oh)).json()["id"]
    their_bu = (await o.post(BUS, json={"name": "Theirs"}, headers=oh)).json()["id"]
    their_user = other_org.admin.user_id
    their_inv = (
        await o.post(f"{USERS}/invite", json={"email": "t@example.com", "role_ids": [their_role]}, headers=oh)
    ).json()["id"]

    checks: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", f"{ROLES}/{their_role}", None),
        ("PATCH", f"{ROLES}/{their_role}", {"name": "Mine"}),
        ("DELETE", f"{ROLES}/{their_role}", None),
        ("GET", f"{TEAMS}/{their_team}", None),
        ("PATCH", f"{TEAMS}/{their_team}", {"name": "Mine"}),
        ("DELETE", f"{TEAMS}/{their_team}", None),
        ("PUT", f"{TEAMS}/{their_team}/members/{org.admin.user_id}", {}),
        ("DELETE", f"{TEAMS}/{their_team}/members/{their_user}", None),
        ("GET", f"{BUS}/{their_bu}", None),
        ("PATCH", f"{BUS}/{their_bu}", {"name": "Mine"}),
        ("DELETE", f"{BUS}/{their_bu}", None),
        ("GET", f"{USERS}/{their_user}", None),
        ("PATCH", f"{USERS}/{their_user}", {"name": "Mine"}),
        ("POST", f"{USERS}/{their_user}/deactivate", None),
        ("POST", f"{USERS}/{their_user}/reactivate", None),
        ("POST", f"/api/v1/admin/invitations/{their_inv}/resend", None),
        ("POST", f"/api/v1/admin/invitations/{their_inv}/revoke", None),
    ]
    for method, path, body in checks:
        resp = await c.request(method, path, json=body, headers=h)
        assert resp.status_code == 404, (method, path, resp.text)
    # own team + their user -> 404 as well
    my_team = (await c.post(TEAMS, json={"name": "Mine"}, headers=h)).json()["id"]
    assert (await c.put(f"{TEAMS}/{my_team}/members/{their_user}", json={}, headers=h)).status_code == 404

    # their ids in request bodies are unknown references
    mine = await join(app, org.tenant, "viewer")
    await mine.client.aclose()
    ref_bodies: list[dict[str, object]] = [
        {"role_ids": [their_role]},
        {"team_ids": [their_team]},
        {"business_unit_id": their_bu},
    ]
    for ref_body in ref_bodies:
        resp = await c.patch(f"{USERS}/{mine.user_id}", json=ref_body, headers=h)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "invalid_reference"
    invite = await c.post(
        f"{USERS}/invite", json={"email": "z@example.com", "role_ids": [their_role]}, headers=h
    )
    assert invite.status_code == 422

    # lists only show own rows
    assert {r["key"] for r in (await c.get(ROLES)).json()} == DEFAULT_ROLE_KEYS
    assert [t["name"] for t in (await c.get(TEAMS)).json()["items"]] == ["Mine"]
    assert (await c.get(BUS)).json()["total"] == 0
    assert {u["id"] for u in (await c.get(USERS)).json()["items"]} == {
        str(org.admin.user_id),
        str(mine.user_id),
    }
    assert (await c.get("/api/v1/admin/invitations")).json()["total"] == 0
    # and nothing of theirs changed
    assert (await o.get(f"{ROLES}/{their_role}")).json()["name"] == "Theirs"


async def test_seed_default_roles_is_idempotent() -> None:
    tenant = await make_tenant()
    async with tenant_session(tenant.id) as db:
        first = await access.seed_default_roles(db, tenant.id)
        second = await access.seed_default_roles(db, tenant.id)
    assert first == second
    assert set(first) == DEFAULT_ROLE_KEYS


async def test_tenant_admin_gets_permissions_added_to_catalog_later(
    org: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = PermissionDef("widget:frobnicate", "Test", "A permission added after seeding")
    monkeypatch.setitem(CATALOG, fake.key, fake)
    me = (await org.admin.client.get(ME)).json()
    assert me["permissions"]["widget:frobnicate"] == "tenant"
    async with tenant_session(org.tenant.id) as db:
        stored = (await db.execute(select(Role.permissions).where(Role.key == "tenant_admin"))).scalar_one()
    assert "widget:frobnicate" not in stored


async def test_team_members_hide_contact_details_without_user_read(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    team_id = (await c.post(TEAMS, json={"name": "Private"}, headers=h)).json()["id"]
    recruiter = await join(app, org.tenant, "recruiter")
    try:
        assert (
            await c.put(f"{TEAMS}/{team_id}/members/{recruiter.user_id}", json={}, headers=h)
        ).status_code == 200
        (full,) = (await c.get(f"{TEAMS}/{team_id}")).json()["members"]
        assert full["email"] == recruiter.email
        assert full["is_active"] is True
        (hidden,) = (await recruiter.client.get(f"{TEAMS}/{team_id}")).json()["members"]
        assert hidden["email"] is None
        assert hidden["is_active"] is None
        assert hidden["name"]
    finally:
        await recruiter.client.aclose()


async def test_role_update_checks_old_and_new_grants_separately(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    editor_perms = {"role:read": "tenant", "role:manage": "tenant", "candidate:read": "own"}
    assert (
        await c.post(ROLES, json={"key": "editor", "name": "Editor", "permissions": editor_perms}, headers=h)
    ).status_code == 201
    target = (
        await c.post(
            ROLES,
            json={"key": "wide_reader", "name": "Wide", "permissions": {"candidate:read": "tenant"}},
            headers=h,
        )
    ).json()
    editor = await join(app, org.tenant, "editor")
    try:
        # tenant -> own passes a merged check ({**old, **new}); the old grant is checked on its own too
        resp = await editor.client.patch(
            f"{ROLES}/{target['id']}", json={"permissions": {"candidate:read": "own"}}, headers=editor.h()
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["details"]["permission"] == "candidate:read"
        mine = (
            await c.post(
                ROLES,
                json={"key": "own_reader", "name": "Own", "permissions": {"candidate:read": "own"}},
                headers=h,
            )
        ).json()
        ok = await editor.client.patch(f"{ROLES}/{mine['id']}", json={"permissions": {}}, headers=editor.h())
        assert ok.status_code == 200
    finally:
        await editor.client.aclose()
