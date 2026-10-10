"""Story 1.5: user administration -- invitations (invite/resend/revoke, seat limit), edit, deactivate /
reactivate, last-admin protection, and the operator's resend/revoke of a tenant's admin invitation."""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import platform_session, tenant_session
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.core.permissions import CATALOG
from app.core.settings import get_settings
from app.models.identity import Invitation, Role, Tenant, User
from app.models.identity import Session as AuthSession
from app.services import access_service as access

from .conftest import USER_PASSWORD, Member, Org, csrf, invite, join, new_client, tenant_body

USERS = "/api/v1/admin/users"
INVITATIONS = "/api/v1/admin/invitations"
ROLES = "/api/v1/admin/roles"
TEAMS = "/api/v1/admin/teams"
BUS = "/api/v1/admin/business-units"
ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
ACCEPT = "/api/v1/auth/invitations/{}/accept"
INVITE_RE = re.compile(re.escape(get_settings().frontend_url.rstrip("/")) + r"/accept-invite#token=(\S+)")


def _mails(tenant_id: uuid.UUID, to: str | None = None) -> list[EmailMessage]:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return [m for m in mailer.sent if m.tenant_id == tenant_id and (to is None or m.to == to)]


def _token(mail: EmailMessage) -> str:
    match = INVITE_RE.search(mail.text)
    assert match, mail.text
    return match.group(1)


async def _role_id(tenant_id: uuid.UUID, key: str) -> str:
    async with tenant_session(tenant_id) as db:
        return str((await db.execute(select(Role.id).where(Role.key == key))).scalar_one())


async def _accept(app: FastAPI, token: str, name: str = "New Person") -> httpx.Response:
    async with new_client(app) as client:
        return await client.post(ACCEPT.format(token), json={"name": name, "password": USER_PASSWORD})


async def _invite(org: Org, email: str, *role_keys: str, **extra: object) -> httpx.Response:
    role_ids = [await _role_id(org.tenant.id, k) for k in (role_keys or ("recruiter",))]
    return await org.admin.client.post(
        f"{USERS}/invite", json={"email": email, "role_ids": role_ids, **extra}, headers=org.admin.h()
    )


# ---------------------------------------------------------------- invite
async def test_invite_accept_assigns_roles_bu_and_teams(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    bu_id = (await c.post(BUS, json={"name": "West"}, headers=h)).json()["id"]
    team_id = (await c.post(TEAMS, json={"name": "Delta"}, headers=h)).json()["id"]
    resp = await _invite(
        org, "New.Hire@Initech.example.com", "recruiter", "interviewer",
        name="Nia Hire", business_unit_id=bu_id, team_ids=[team_id],
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    inv = resp.json()
    assert inv["email"] == "new.hire@initech.example.com"
    assert (inv["name"], inv["business_unit_id"], inv["team_ids"]) == ("Nia Hire", bu_id, [team_id])
    assert inv["invited_by_type"] == "user"
    assert inv["expired"] is False

    (mail,) = _mails(org.tenant.id, "new.hire@initech.example.com")
    assert mail.kind == "user_invitation"
    token = _token(mail)
    async with new_client(app) as anon:
        preview = await anon.get(f"/api/v1/auth/invitations/{token}")
    assert preview.json()["name"] == "Nia Hire"

    accepted = await _accept(app, token)
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["user"]["role_keys"] == ["interviewer", "recruiter"]
    assert body["user"]["business_unit_id"] == bu_id
    assert (
        body["permissions"]["candidate:read"] == "tenant"
    )  # widest of recruiter (tenant) / interviewer (own)
    # widest of recruiter (team, since Epic 7) / interviewer (own)
    assert body["permissions"]["scorecard:submit"] == "team"

    user = (await c.get(f"{USERS}/{body['user']['id']}")).json()
    assert user["business_unit"] == {"id": bu_id, "name": "West"}
    assert [t["id"] for t in user["teams"]] == [team_id]
    assert {r["key"] for r in user["roles"]} == {"recruiter", "interviewer"}
    assert (await c.get(INVITATIONS)).json()["total"] == 0


async def test_invite_conflicts_and_validation(org: Org) -> None:
    dup_user = await _invite(org, org.admin.email.upper())
    assert dup_user.status_code == 409
    assert dup_user.json()["error"]["code"] == "user_exists"

    assert (await _invite(org, "p@example.com")).status_code == 201
    pending = await _invite(org, "P@example.com")
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "invitation_pending"

    no_roles = await org.admin.client.post(
        f"{USERS}/invite", json={"email": "q@example.com", "role_ids": []}, headers=org.admin.h()
    )
    assert no_roles.status_code == 422
    unknown = await org.admin.client.post(
        f"{USERS}/invite",
        json={"email": "q@example.com", "role_ids": [str(uuid.uuid4())]},
        headers=org.admin.h(),
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_reference"
    assert unknown.json()["error"]["details"]["fields"][0]["field"] == "role_ids"


async def test_seat_limit_counts_active_users_and_pending_invitations(app: FastAPI, org: Org) -> None:
    async with platform_session() as db:
        await db.execute(update(Tenant).where(Tenant.id == org.tenant.id).values(seat_limit=3))
    member = await join(app, org.tenant, "viewer")  # 2 active
    await member.client.aclose()
    assert (await _invite(org, "third@example.com")).status_code == 201  # 2 active + 1 pending = 3
    full = await _invite(org, "fourth@example.com")
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "seat_limit_reached"

    # revoking the pending invitation frees the seat
    (pending,) = (await org.admin.client.get(INVITATIONS)).json()["items"]
    revoked = await org.admin.client.post(f"{INVITATIONS}/{pending['id']}/revoke", headers=org.admin.h())
    assert revoked.status_code == 204
    assert (await _invite(org, "fourth@example.com")).status_code == 201

    # a deactivated user cannot be reactivated while the tenant is full
    h = org.admin.h()
    assert (await org.admin.client.post(f"{USERS}/{member.user_id}/deactivate", headers=h)).status_code == 200
    assert (await _invite(org, "fifth@example.com")).status_code == 201  # 1 active + 2 pending
    blocked = await org.admin.client.post(f"{USERS}/{member.user_id}/reactivate", headers=h)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "seat_limit_reached"


# ---------------------------------------------------------------- resend / revoke
async def test_resend_rotates_token_and_revoke_invalidates(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    inv = (await _invite(org, "resend@example.com")).json()
    (first,) = _mails(org.tenant.id, "resend@example.com")
    old_token = _token(first)

    resent = await c.post(f"{INVITATIONS}/{inv['id']}/resend", headers=h)
    assert resent.status_code == 200, resent.text
    assert resent.json()["expires_at"] >= inv["expires_at"]
    _, second = _mails(org.tenant.id, "resend@example.com")
    new_token = _token(second)
    assert new_token != old_token
    assert (await _accept(app, old_token)).status_code == 410
    accepted = await _accept(app, new_token)
    assert accepted.status_code == 201, accepted.text

    again = await c.post(f"{INVITATIONS}/{inv['id']}/resend", headers=h)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invitation_accepted"
    revoke_accepted = await c.post(f"{INVITATIONS}/{inv['id']}/revoke", headers=h)
    assert revoke_accepted.status_code == 409

    other = (await _invite(org, "revoke@example.com")).json()
    token = _token(_mails(org.tenant.id, "revoke@example.com")[0])
    assert (await c.post(f"{INVITATIONS}/{other['id']}/revoke", headers=h)).status_code == 204
    assert (await c.post(f"{INVITATIONS}/{other['id']}/revoke", headers=h)).status_code == 204  # idempotent
    assert (await _accept(app, token)).status_code == 410
    async with new_client(app) as anon:
        assert (await anon.get(f"/api/v1/auth/invitations/{token}")).status_code == 410
    revoked_resend = await c.post(f"{INVITATIONS}/{other['id']}/resend", headers=h)
    assert revoked_resend.json()["error"]["code"] == "invitation_revoked"
    assert (await c.get(INVITATIONS)).json()["total"] == 0
    assert (await c.post(f"{INVITATIONS}/{uuid.uuid4()}/resend", headers=h)).status_code == 404


# ---------------------------------------------------------------- deactivate / reactivate
async def test_deactivate_revokes_sessions_and_blocks_login(app: FastAPI, org: Org) -> None:
    member = await join(app, org.tenant, "recruiter")
    try:
        assert (await member.client.get(ME)).status_code == 200
        resp = await org.admin.client.post(f"{USERS}/{member.user_id}/deactivate", headers=org.admin.h())
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False
        async with platform_session() as db:
            sessions = (
                (await db.execute(select(AuthSession).where(AuthSession.principal_id == member.user_id)))
                .scalars()
                .all()
            )
        assert sessions
        assert all(s.revoked_at is not None for s in sessions)
        assert (await member.client.get(ME)).status_code == 401
        login = await member.client.post(
            LOGIN, json={"tenant_slug": member.slug, "email": member.email, "password": USER_PASSWORD}
        )
        assert login.status_code == 401

        listed = (await org.admin.client.get(USERS, params={"is_active": "false"})).json()
        assert [u["id"] for u in listed["items"]] == [str(member.user_id)]

        back = await org.admin.client.post(f"{USERS}/{member.user_id}/reactivate", headers=org.admin.h())
        assert back.status_code == 200
        assert back.json()["is_active"] is True
        login = await member.client.post(
            LOGIN, json={"tenant_slug": member.slug, "email": member.email, "password": USER_PASSWORD}
        )
        assert login.status_code == 200
    finally:
        await member.client.aclose()


async def test_self_deactivate_and_last_admin(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    own = await c.post(f"{USERS}/{org.admin.user_id}/deactivate", headers=h)
    assert own.status_code == 409
    assert own.json()["error"]["code"] == "cannot_deactivate_self"

    # A user manager (custom role) cannot remove the only Tenant Admin.
    role = await c.post(
        ROLES,
        json={
            "key": "user_manager",
            "name": "User manager",
            "permissions": {"user:read": "tenant", "user:manage": "tenant"},
        },
        headers=h,
    )
    assert role.status_code == 201
    manager: Member = await join(app, org.tenant, "user_manager")
    try:
        mh = manager.h()
        last = await manager.client.post(f"{USERS}/{org.admin.user_id}/deactivate", headers=mh)
        # deactivating needs every permission the target's roles carry
        assert last.status_code == 403
        assert last.json()["error"]["code"] == "forbidden"
        demote = await manager.client.patch(
            f"{USERS}/{org.admin.user_id}", json={"role_ids": [role.json()["id"]]}, headers=mh
        )
        # removing Tenant Admin needs every permission it carries (no escalation / de-escalation)
        assert demote.status_code == 403
        assert demote.json()["error"]["code"] == "forbidden"
        self_demote = await c.patch(f"{USERS}/{org.admin.user_id}", json={"role_ids": []}, headers=h)
        assert self_demote.json()["error"]["code"] == "last_admin"

        # With a second admin, the first may be demoted / deactivated.
        admin_role = await _role_id(org.tenant.id, "tenant_admin")
        promoted = await c.patch(
            f"{USERS}/{manager.user_id}", json={"role_ids": [admin_role], "name": "Max Manager"}, headers=h
        )
        assert promoted.status_code == 200
        assert promoted.json()["name"] == "Max Manager"
        assert promoted.json()["role_keys"] == ["tenant_admin"]
        gone = await manager.client.post(f"{USERS}/{org.admin.user_id}/deactivate", headers=mh)
        assert gone.status_code == 200, gone.text
        now_last = await manager.client.patch(f"{USERS}/{manager.user_id}", json={"role_ids": []}, headers=mh)
        assert now_last.json()["error"]["code"] == "last_admin"
    finally:
        await manager.client.aclose()


async def test_list_users_filters_and_patch(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    viewer = await join(app, org.tenant, "viewer", name="Victor Viewer")
    await viewer.client.aclose()
    viewer_role = await _role_id(org.tenant.id, "viewer")
    by_role = (await c.get(USERS, params={"role_id": viewer_role})).json()
    assert [u["name"] for u in by_role["items"]] == ["Victor Viewer"]
    by_q = (await c.get(USERS, params={"q": "victor"})).json()
    assert by_q["total"] == 1
    assert (await c.get(USERS, params={"q": "100%_"})).json()["total"] == 0
    assert (await c.get(USERS)).json()["total"] == 2

    bu_id = (await c.post(BUS, json={"name": "East"}, headers=h)).json()["id"]
    team_id = (await c.post(TEAMS, json={"name": "Omega"}, headers=h)).json()["id"]
    patched = await c.patch(
        f"{USERS}/{viewer.user_id}", json={"business_unit_id": bu_id, "team_ids": [team_id]}, headers=h
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["business_unit"]["id"] == bu_id
    assert (await c.get(USERS, params={"business_unit_id": bu_id})).json()["total"] == 1
    cleared = await c.patch(
        f"{USERS}/{viewer.user_id}", json={"business_unit_id": None, "team_ids": []}, headers=h
    )
    assert cleared.json()["business_unit"] is None
    assert cleared.json()["teams"] == []
    extra = await c.patch(f"{USERS}/{viewer.user_id}", json={"email": "x@example.com"}, headers=h)
    assert extra.status_code == 422
    assert (await c.get(f"{USERS}/{uuid.uuid4()}")).status_code == 404


# ---------------------------------------------------------------- operator resend / revoke (from 1.2)
async def test_operator_resend_and_revoke_tenant_admin_invitation(
    op_client: httpx.AsyncClient, app: FastAPI
) -> None:
    body = tenant_body()
    created = await op_client.post("/api/v1/platform/tenants", json=body, headers=csrf(op_client))
    tenant_id = uuid.UUID(created.json()["id"])
    admin_email = str(body["admin_email"]).lower()
    base = f"/api/v1/platform/tenants/{tenant_id}/invitations"
    (first,) = _mails(tenant_id, admin_email)

    resent = await op_client.post(f"{base}/resend", headers=csrf(op_client))
    assert resent.status_code == 200, resent.text
    assert resent.json()["invited_by_type"] == "platform"
    _, second = _mails(tenant_id, admin_email)
    assert second.kind == "tenant_admin_invitation"
    assert (await _accept(app, _token(first))).status_code == 410

    assert (await op_client.post(f"{base}/revoke", headers=csrf(op_client))).status_code == 204
    assert (await _accept(app, _token(second))).status_code == 410
    again = await op_client.post(f"{base}/revoke", headers=csrf(op_client))
    assert again.json()["error"]["code"] == "no_pending_invitation"

    # resend re-issues even a revoked invitation; accepting it makes the admin
    assert (await op_client.post(f"{base}/resend", headers=csrf(op_client))).status_code == 200
    third = _mails(tenant_id, admin_email)[-1]
    accepted = await _accept(app, _token(third))
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["user"]["role_keys"] == ["tenant_admin"]
    done = await op_client.post(f"{base}/resend", headers=csrf(op_client))
    assert done.status_code == 409
    assert done.json()["error"]["code"] == "invitation_accepted"

    missing = await op_client.post(
        f"/api/v1/platform/tenants/{uuid.uuid4()}/invitations/resend", headers=csrf(op_client)
    )
    assert missing.status_code == 404
    async with new_client(app) as anon:
        assert (await anon.post(f"{base}/resend")).status_code == 401
    async with tenant_session(tenant_id) as db:
        invitations = (await db.execute(select(Invitation))).scalars().all()
    assert len(invitations) == 1  # always the same invitation, rotated


async def test_tenant_user_cannot_use_operator_resend(org: Org) -> None:
    resp = await org.admin.client.post(
        f"/api/v1/platform/tenants/{org.tenant.id}/invitations/resend", headers=org.admin.h()
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------- no privilege escalation
async def test_managers_cannot_grant_more_than_they_hold(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    perms = {"user:read": "tenant", "user:manage": "tenant", "role:read": "tenant", "role:manage": "tenant"}
    role = (
        await c.post(ROLES, json={"key": "people_ops", "name": "People ops", "permissions": perms}, headers=h)
    ).json()
    ops = await join(app, org.tenant, "people_ops")
    try:
        oh = ops.h()
        admin_role = await _role_id(org.tenant.id, "tenant_admin")
        viewer_role = await _role_id(org.tenant.id, "viewer")

        self_promote = await ops.client.patch(
            f"{USERS}/{ops.user_id}", json={"role_ids": [role["id"], admin_role]}, headers=oh
        )
        assert self_promote.status_code == 403
        assert self_promote.json()["error"]["details"]["permission"]
        invite_admin = await ops.client.post(
            f"{USERS}/invite", json={"email": "boss@example.com", "role_ids": [admin_role]}, headers=oh
        )
        assert invite_admin.status_code == 403
        invite_viewer = await ops.client.post(
            f"{USERS}/invite", json={"email": "look@example.com", "role_ids": [viewer_role]}, headers=oh
        )
        assert invite_viewer.status_code == 403  # viewer reads candidates; people_ops does not

        wide = await ops.client.post(
            ROLES,
            json={"key": "wide", "name": "Wide", "permissions": {"candidate:read": "tenant"}},
            headers=oh,
        )
        assert wide.status_code == 403
        assert wide.json()["error"]["details"]["permission"] == "candidate:read"
        own_role = await ops.client.patch(
            f"{ROLES}/{role['id']}", json={"permissions": {**perms, "audit:read": "tenant"}}, headers=oh
        )
        assert own_role.status_code == 403
        narrow = await ops.client.post(
            ROLES,
            json={"key": "reader", "name": "Reader", "permissions": {"user:read": "tenant"}},
            headers=oh,
        )
        assert narrow.status_code == 201
        ok = await ops.client.post(
            f"{USERS}/invite", json={"email": "r@example.com", "role_ids": [narrow.json()["id"]]}, headers=oh
        )
        assert ok.status_code == 201
    finally:
        await ops.client.aclose()


# ---------------------------------------------------------------- review fixes
async def _set_seat_limit(tenant_id: uuid.UUID, seats: int) -> None:
    async with platform_session() as db:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(seat_limit=seats))


async def test_last_admin_on_deactivate_by_equally_privileged_user(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    everything = dict.fromkeys(CATALOG, "tenant")
    created = await c.post(
        ROLES, json={"key": "superuser", "name": "Super", "permissions": everything}, headers=h
    )
    assert created.status_code == 201
    su = await join(app, org.tenant, "superuser")
    try:
        last = await su.client.post(f"{USERS}/{org.admin.user_id}/deactivate", headers=su.h())
        assert last.status_code == 409
        assert last.json()["error"]["code"] == "last_admin"
    finally:
        await su.client.aclose()


async def test_user_manager_cannot_touch_more_privileged_users_or_invitations(app: FastAPI, org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    perms = {"user:read": "tenant", "user:manage": "tenant"}
    assert (
        await c.post(ROLES, json={"key": "hr", "name": "HR", "permissions": perms}, headers=h)
    ).status_code == 201
    hr = await join(app, org.tenant, "hr")
    viewer = await join(app, org.tenant, "viewer")
    await viewer.client.aclose()
    try:
        hh = hr.h()
        for action in ("deactivate", "reactivate"):
            resp = await hr.client.post(f"{USERS}/{org.admin.user_id}/{action}", headers=hh)
            assert resp.status_code == 403, resp.text
        inv = (await _invite(org, "admin2@example.com", "tenant_admin")).json()
        for action in ("resend", "revoke"):
            resp = await hr.client.post(f"{INVITATIONS}/{inv['id']}/{action}", headers=hh)
            assert resp.status_code == 403, resp.text
        # team / business unit changes need team:manage / business_unit:manage
        team_id = (await c.post(TEAMS, json={"name": "Gamma"}, headers=h)).json()["id"]
        bu_id = (await c.post(BUS, json={"name": "Central"}, headers=h)).json()["id"]
        no_team = await hr.client.patch(f"{USERS}/{viewer.user_id}", json={"team_ids": [team_id]}, headers=hh)
        assert no_team.status_code == 403
        assert no_team.json()["error"]["details"]["permission"] == "team:manage"
        no_bu = await hr.client.patch(
            f"{USERS}/{viewer.user_id}", json={"business_unit_id": bu_id}, headers=hh
        )
        assert no_bu.json()["error"]["details"]["permission"] == "business_unit:manage"
        assert (
            await hr.client.patch(f"{USERS}/{viewer.user_id}", json={"name": "V"}, headers=hh)
        ).status_code == 200
    finally:
        await hr.client.aclose()


async def test_no_self_team_or_business_unit_changes(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    team_id = (await c.post(TEAMS, json={"name": "Self"}, headers=h)).json()["id"]
    bu_id = (await c.post(BUS, json={"name": "Self BU"}, headers=h)).json()["id"]
    me = f"{USERS}/{org.admin.user_id}"
    for body in ({"team_ids": [team_id]}, {"business_unit_id": bu_id}):
        resp = await c.patch(me, json=body, headers=h)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "self_scope_change"
    member = await c.put(f"{TEAMS}/{team_id}/members/{org.admin.user_id}", json={}, headers=h)
    assert member.status_code == 409
    assert member.json()["error"]["code"] == "self_scope_change"
    assert (await c.patch(me, json={"name": "Ada A."}, headers=h)).status_code == 200


async def test_resend_of_expired_invitation_needs_a_free_seat(org: Org) -> None:
    await _set_seat_limit(org.tenant.id, 2)
    inv = (await _invite(org, "late@example.com")).json()  # 1 active + 1 pending = 2 (full)
    ok = await org.admin.client.post(f"{INVITATIONS}/{inv['id']}/resend", headers=org.admin.h())
    assert ok.status_code == 200  # still pending: it already holds its seat
    async with tenant_session(org.tenant.id) as db:
        await db.execute(
            update(Invitation)
            .where(Invitation.id == uuid.UUID(inv["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
    await invite(org.tenant.id, "squatter@example.com", "viewer")  # takes the freed seat
    full = await org.admin.client.post(f"{INVITATIONS}/{inv['id']}/resend", headers=org.admin.h())
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "seat_limit_reached"


async def test_concurrent_accepts_cannot_exceed_seat_limit(app: FastAPI, org: Org) -> None:
    """A seat taken by a concurrent, still-uncommitted transaction is seen by the accept (tenant row lock)."""
    await _set_seat_limit(org.tenant.id, 2)  # the admin holds one seat
    token = await invite(org.tenant.id, "race@example.com", "viewer")
    async with tenant_session(org.tenant.id) as db:
        await access.lock_tenant(db, org.tenant.id)
        db.add(User(tenant_id=org.tenant.id, email="rival@example.com", name="Rival", password_hash="x"))
        await db.flush()
        pending = asyncio.create_task(_accept(app, token))
        await asyncio.sleep(0.5)
        assert not pending.done()  # blocked on the tenant row until the rival commits
    resp = await pending
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "seat_limit_reached"
