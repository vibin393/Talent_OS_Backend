"""Story 1.6: automatic audit log -- listener, explicit events, PII encryption + crypto-shredding,
append-only grants, tenant isolation and the tenant-admin search / CSV export API."""

import csv
import io
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core import audit
from app.core.database import platform_session, tenant_session
from app.core.mailer import OutboxMailer, get_mailer
from app.core.settings import get_settings
from app.models.identity import User
from tests.modules.identity.conftest import USER_PASSWORD, Member, Org, join, make_tenant

LOGS = "/api/v1/admin/audit-logs"
EXPORT = "/api/v1/admin/audit-logs/export.csv"
USERS = "/api/v1/admin/users"
RESET_RE = re.compile(re.escape(get_settings().frontend_url.rstrip("/")) + r"/reset-password#token=(\S+)")


@pytest.fixture
async def org(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Ada Admin")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


@pytest.fixture
async def member(app: FastAPI, org: Org) -> AsyncIterator[Member]:
    m = await join(app, org.tenant, "recruiter", name="Rita Recruiter")
    yield m
    await m.client.aclose()


async def _raw_rows(owner_db: AsyncEngine, entity_id: uuid.UUID | str) -> list[Any]:
    """Rows as stored (owner role, bypassing RLS) -- to inspect encryption at rest."""
    async with owner_db.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM audit_logs WHERE entity_id = :e ORDER BY occurred_at"), {"e": str(entity_id)}
        )
        return list(result.mappings().all())


async def _logs(member: Member, **params: str) -> dict[str, Any]:
    resp = await member.client.get(LOGS, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


# ---------------------------------------------------------------- listener: update, encryption
async def test_rename_user_writes_one_encrypted_row_decrypted_by_api(
    org: Org, member: Member, owner_db: AsyncEngine
) -> None:
    resp = await org.admin.client.patch(
        f"{USERS}/{member.user_id}", json={"name": "Rita Renamed"}, headers=org.admin.h()
    )
    assert resp.status_code == 200, resp.text
    request_id = resp.headers["x-request-id"]

    raw = [r for r in await _raw_rows(owner_db, member.user_id) if r["action"] == "user.updated"]
    assert len(raw) == 1
    stored = raw[0]
    assert set(stored["changes"]) == {"name"}
    assert "Rita" not in json.dumps(stored["changes"])  # encrypted at rest
    assert stored["changes"]["name"]["new"]["$subject"] == f"user:{member.user_id}"
    assert (stored["tenant_id"], stored["actor_type"]) == (org.tenant.id, "user")
    assert stored["actor_id"] == org.admin.user_id
    assert stored["request_id"] == request_id
    assert stored["ip"]

    body = await _logs(org.admin, entity_type="user", entity_id=str(member.user_id), action="user.updated")
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["changes"] == {"name": {"old": "Rita Recruiter", "new": "Rita Renamed"}}
    assert item["actor_id"] == str(org.admin.user_id)
    assert item["request_id"] == request_id


async def test_user_creation_is_audited_with_encrypted_email(org: Org, member: Member) -> None:
    body = await _logs(org.admin, entity_id=str(member.user_id), action="user.created")
    (item,) = body["items"]
    assert item["changes"]["email"]["new"] == member.email
    assert item["changes"]["name"]["new"] == "Rita Recruiter"
    assert (item["actor_type"], item["actor_id"]) == ("user", str(member.user_id))  # self sign-up
    assert not any("password" in k or "hash" in k for k in item["changes"])
    roles = await _logs(org.admin, entity_type="user_role", entity_id=str(member.user_id))
    assert roles["total"] == 1
    assert (await _logs(org.admin, action="auth.invitation_accepted", entity_id=str(member.user_id)))[
        "total"
    ] == 1


async def test_password_reset_audit_never_contains_the_hash(
    org: Org, member: Member, client: httpx.AsyncClient, owner_db: AsyncEngine
) -> None:
    resp = await client.post(
        "/api/v1/auth/password-reset/request", json={"tenant_slug": org.tenant.slug, "email": member.email}
    )
    assert resp.status_code == 202, resp.text
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    (mail,) = [m for m in mailer.sent if m.to == member.email and m.kind == "password_reset"]
    match = RESET_RE.search(mail.text)
    assert match
    confirm = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": match.group(1), "password": USER_PASSWORD + "!x"},
    )
    assert confirm.status_code == 204, confirm.text

    rows = await _raw_rows(owner_db, member.user_id)
    actions = [r["action"] for r in rows]
    assert "auth.password_reset_completed" in actions
    for row in rows:
        dumped = json.dumps(row["changes"] or {}) + json.dumps(row["data"] or {})
        assert "password_hash" not in dumped
        assert "argon2" not in dumped


# ---------------------------------------------------------------- failure semantics
async def test_audit_failure_rolls_back_the_business_change(
    org: Org, member: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = audit._new_row

    def failing(session: Any, **kw: Any) -> audit.AuditLog:
        if kw.get("action") == "user.updated":
            msg = "audit store unavailable"
            raise RuntimeError(msg)
        return original(session, **kw)

    monkeypatch.setattr(audit, "_new_row", failing)
    resp = await org.admin.client.patch(
        f"{USERS}/{member.user_id}", json={"name": "Never Saved"}, headers=org.admin.h()
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    async with tenant_session(org.tenant.id) as db:
        user = await db.get(User, member.user_id)
        assert user is not None
        assert user.name == "Rita Recruiter"


# ---------------------------------------------------------------- tamper resistance
@pytest.mark.parametrize(
    "statement", ["UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs", "TRUNCATE audit_logs"]
)
async def test_runtime_role_cannot_modify_audit_rows(org: Org, statement: str) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(org.tenant.id) as db:
            await db.execute(text(statement))


async def test_runtime_role_cannot_delete_subject_keys(org: Org) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(org.tenant.id) as db:
            await db.execute(text("DELETE FROM subject_keys"))


async def test_tenant_session_cannot_write_platform_or_foreign_rows(org: Org, other: Org) -> None:
    async with tenant_session(org.tenant.id) as db:
        with pytest.raises(ValueError, match="tenant"):
            audit.record("x.y", session=db, tenant_id=other.tenant.id)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(org.tenant.id) as db:
            await db.execute(
                text(
                    "INSERT INTO audit_logs (id, tenant_id, occurred_at, actor_type, action) "
                    "VALUES (gen_random_uuid(), NULL, now(), 'system', 'smuggled')"
                )
            )


# ---------------------------------------------------------------- crypto-shredding
async def test_shred_erases_subject_pii_in_every_row(org: Org, member: Member) -> None:
    resp = await org.admin.client.patch(
        f"{USERS}/{member.user_id}", json={"name": "Before Shred"}, headers=org.admin.h()
    )
    assert resp.status_code == 200
    async with tenant_session(org.tenant.id) as db:
        await audit.shred("user", member.user_id, session=db)
        await audit.shred("user", member.user_id, session=db)  # idempotent

    body = await _logs(org.admin, entity_type="user", entity_id=str(member.user_id))
    created = next(i for i in body["items"] if i["action"] == "user.created")
    updated = next(i for i in body["items"] if i["action"] == "user.updated")
    assert created["changes"]["email"]["new"] == audit.ERASED
    assert created["changes"]["name"]["new"] == audit.ERASED
    assert updated["changes"]["name"] == {"old": audit.ERASED, "new": audit.ERASED}
    assert created["changes"]["tenant_id"]["new"] == str(org.tenant.id)  # non-PII untouched
    # Later changes of a shredded subject are stored erased.
    again = await org.admin.client.patch(
        f"{USERS}/{member.user_id}", json={"name": "After Shred"}, headers=org.admin.h()
    )
    assert again.status_code == 200
    latest = (await _logs(org.admin, entity_id=str(member.user_id), action="user.updated"))["items"][0]
    assert latest["changes"]["name"] == {"old": audit.ERASED, "new": audit.ERASED}
    shredded = await _logs(org.admin, action="audit.subject_shredded")
    assert shredded["total"] == 2


# ---------------------------------------------------------------- isolation and permissions
@pytest.fixture
async def other(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Olga Other")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


async def test_cross_tenant_admin_never_sees_rows(org: Org, member: Member, other: Org) -> None:
    mine = await _logs(org.admin, entity_id=str(member.user_id))
    assert mine["total"] >= 2
    theirs = await _logs(other.admin, entity_id=str(member.user_id))
    assert theirs["total"] == 0
    everything = await _logs(other.admin, page_size="100")
    assert all(i["actor_id"] != str(org.admin.user_id) for i in everything["items"])
    csv_resp = await other.admin.client.get(EXPORT, params={"entity_id": str(member.user_id)})
    assert csv_resp.status_code == 200
    assert csv_resp.text.strip().count("\n") == 0  # header only


async def test_platform_rows_are_invisible_to_tenants_and_tenant_rows_to_platform(
    org: Org, member: Member
) -> None:
    marker = uuid.uuid4()
    async with platform_session() as db:
        audit.record("platform.test_event", "thing", marker, session=db)
    async with tenant_session(org.tenant.id) as db:
        rows = (await db.execute(select(audit.AuditLog).where(audit.AuditLog.entity_id == str(marker)))).all()
        assert rows == []
    async with platform_session() as db:
        rows = (
            await db.execute(select(audit.AuditLog).where(audit.AuditLog.entity_id == str(member.user_id)))
        ).all()
        assert rows == []


async def test_recruiter_without_audit_read_gets_403(member: Member) -> None:
    for url in (LOGS, EXPORT):
        resp = await member.client.get(url)
        assert resp.status_code == 403
        assert resp.json()["error"]["details"]["permission"] == "audit:read"


async def test_unauthenticated_gets_401(client: httpx.AsyncClient) -> None:
    assert (await client.get(LOGS)).status_code == 401


# ---------------------------------------------------------------- search and export
async def test_filters_and_newest_first(org: Org, member: Member) -> None:
    for name in ("First Rename", "Second Rename"):
        resp = await org.admin.client.patch(
            f"{USERS}/{member.user_id}", json={"name": name}, headers=org.admin.h()
        )
        assert resp.status_code == 200
    body = await _logs(org.admin, action="user.upd", actor_id=str(org.admin.user_id))
    assert [i["changes"]["name"]["new"] for i in body["items"]][:2] == ["Second Rename", "First Rename"]
    assert all(i["action"] == "user.updated" for i in body["items"])
    newest = body["items"][0]["occurred_at"]
    assert (await _logs(org.admin, action="user.updated", to=newest))["total"] == body["total"] - 1
    assert (await _logs(org.admin, action="user.updated", **{"from": newest}))["total"] == 1
    assert (await _logs(org.admin, action="%"))["total"] == 0  # LIKE wildcards are literal


async def test_csv_export_decrypts_and_is_itself_audited(org: Org, member: Member) -> None:
    resp = await org.admin.client.get(EXPORT, params={"entity_id": str(member.user_id)})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert int(resp.headers["x-export-rows"]) == len(rows) >= 2
    created = next(r for r in rows if r["action"] == "user.created")
    assert json.loads(created["changes"])["email"]["new"] == member.email
    exported = await _logs(org.admin, action="audit.exported")
    assert exported["total"] == 1
    item = exported["items"][0]
    assert item["data"]["rows"] == len(rows)
    assert item["data"]["filters"] == {"entity_id": str(member.user_id)}


def test_csv_cells_are_formula_safe() -> None:
    assert audit._csv_cell("=HYPERLINK()") == "'=HYPERLINK()"
    assert audit._csv_cell({"a": 1}) == '{"a": 1}'
    assert audit._csv_cell(None) == ""


def test_identity_models_are_audited_with_pii_and_exclusions() -> None:
    names = {m.__name__ for m in audit.audited_models()}
    assert names >= {"User", "Invitation", "Role", "Team", "BusinessUnit", "UserRole", "TeamMember"}
    assert User.__pii_fields__ == ("email", "name")
    for column in ("password_hash", "mfa_secret", "updated_at", "token_hash", "last_login_at"):
        assert audit.is_excluded(User, column)
    assert not audit.is_excluded(User, "name")


async def test_mixed_tables_have_forced_rls() -> None:
    from app.core.tenancy import MIXED_TABLES

    async with platform_session() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names) AND relkind = 'r'"
                ),
                {"names": list(MIXED_TABLES)},
            )
        ).all()
    assert {r.relname for r in rows} == set(MIXED_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows)
