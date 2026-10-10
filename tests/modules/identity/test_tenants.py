"""Tenant provisioning by platform operators: tenant + RLS-scoped invitation + outbox email + event."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import select, text, update

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.events import subscribe, unsubscribe
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.core.permissions import DEFAULT_ROLE_KEYS
from app.core.security import Principal, hash_token
from app.core.settings import get_settings
from app.events.identity import TenantProvisioned
from app.models.identity import Invitation, Role, Tenant
from app.schemas.identity import TenantCreate
from app.services import auth_service as service

from .conftest import OperatorCreds, csrf, tenant_body, unique_slug

TENANTS = "/api/v1/platform/tenants"
LINK_RE = re.compile(
    "(" + re.escape(get_settings().frontend_url.rstrip("/")) + r"/accept-invite#token=(\S+))"
)


def _outbox() -> OutboxMailer:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return mailer


def _invitation_mail(tenant_id: str) -> EmailMessage:
    mails = [m for m in _outbox().sent if str(m.tenant_id) == tenant_id]
    assert len(mails) == 1
    return mails[0]


def _token_from(mail: EmailMessage) -> str:
    match = LINK_RE.search(mail.text)
    assert match, mail.text
    return match.group(2)


async def _invitations(as_tenant: uuid.UUID) -> list[Invitation]:
    async with tenant_session(as_tenant) as db:
        return list((await db.execute(select(Invitation))).scalars())


async def test_create_tenant_with_defaults(op_client: httpx.AsyncClient, operator: OperatorCreds) -> None:
    seen: list[TenantProvisioned] = []
    subscribe(TenantProvisioned, seen.append)
    try:
        body = tenant_body()
        resp = await op_client.post(TENANTS, json=body, headers=csrf(op_client))
    finally:
        unsubscribe(TenantProvisioned, seen.append)

    assert resp.status_code == 201, resp.text
    tenant = resp.json()
    assert tenant["slug"] == body["slug"]
    assert tenant["name"] == "Acme Staffing"
    assert (tenant["timezone"], tenant["currency"], tenant["locale"]) == ("Asia/Kolkata", "INR", "en-IN")
    assert tenant["seat_limit"] == 25
    assert tenant["status"] == "active"
    assert tenant["mfa_required"] is False
    assert tenant["created_at"].endswith("Z")
    tenant_id = uuid.UUID(tenant["id"])

    admin_email = str(body["admin_email"]).lower()
    assert seen == [TenantProvisioned(tenant_id=tenant_id, admin_email=admin_email)]

    # Invitation: RLS-scoped to this tenant, single-use token stored hashed, 72 h expiry.
    (inv,) = await _invitations(tenant_id)
    assert inv.email == admin_email
    async with tenant_session(tenant_id) as db:
        roles = {r.key: r for r in (await db.execute(select(Role))).scalars()}
    assert set(roles) == DEFAULT_ROLE_KEYS  # default roles seeded on provisioning
    assert all(r.is_system for r in roles.values())
    assert inv.role_ids == [roles["tenant_admin"].id]
    assert inv.accepted_at is None
    assert (inv.invited_by_type, inv.invited_by_id) == ("platform", operator.id)
    assert timedelta(hours=71, minutes=59) < inv.expires_at - datetime.now(UTC) <= timedelta(hours=72)
    assert await _invitations(uuid.uuid4()) == []
    async with platform_session() as db:
        assert (await db.execute(select(Invitation))).scalars().all() == []

    # Outbox email with the accept link; only the token's hash is in the DB.
    mail = _invitation_mail(tenant["id"])
    assert mail.to == admin_email
    assert mail.kind == "tenant_admin_invitation"
    token = _token_from(mail)
    assert inv.token_hash == hash_token(token)
    assert token not in inv.token_hash
    assert service.parse_invitation_token(token) == tenant_id
    assert len(token.partition(".")[2]) >= 43  # >= 128-bit (here 256-bit) secret
    path = _outbox().outbox_path(mail)
    assert path is not None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["to"] == admin_email
    assert f"/accept-invite#token={token}" in stored["text"]


async def test_create_tenant_with_explicit_settings(op_client: httpx.AsyncClient) -> None:
    body = tenant_body(timezone="Europe/London", currency="gbp", locale="en-GB", slug="  UK-" + unique_slug())
    resp = await op_client.post(TENANTS, json=body, headers=csrf(op_client))
    assert resp.status_code == 201, resp.text
    t = resp.json()
    assert (t["timezone"], t["currency"], t["locale"]) == ("Europe/London", "GBP", "en-GB")
    assert t["slug"] == str(body["slug"]).strip().lower()


async def test_duplicate_slug_case_insensitive_is_409(op_client: httpx.AsyncClient) -> None:
    slug = unique_slug("dup")
    first = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert first.status_code == 201
    sent_before = len(_outbox().sent)

    again = await op_client.post(TENANTS, json=tenant_body(slug=slug.upper()), headers=csrf(op_client))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "slug_taken"
    assert len(_outbox().sent) == sent_before

    # Also enforced by the citext unique constraint (race path), not only the pre-check.
    async with platform_session() as db:
        count = (
            await db.execute(text("SELECT count(*) FROM tenants WHERE slug = :s"), {"s": slug.upper()})
        ).scalar_one()
    assert count == 1


async def test_slug_conflict_from_db_constraint_maps_to_409(
    op_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = unique_slug("race")
    first = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert first.status_code == 201

    # Simulate losing the race: the pre-check sees nothing, the INSERT hits the unique constraint.
    original_select = sa.select

    def blind_select(*args: object) -> object:
        stmt = original_select(*args)  # type: ignore[call-overload]
        if args and args[0] is Tenant.id:
            return stmt.where(text("false"))
        return stmt

    monkeypatch.setattr("app.services.auth_service.select", blind_select)
    resp = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "slug_taken"


@pytest.mark.parametrize(
    "slug", ["Acme Corp!", "www", "API", "admin", "app", "ab", "-acme", "acme-", "a" * 64, "acme_corp", ""]
)
async def test_bad_slug_is_422(op_client: httpx.AsyncClient, slug: str) -> None:
    resp = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_failed"
    fields = [f["field"] for f in resp.json()["error"]["details"]["fields"]]
    assert fields == ["body.slug"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "Mars/Olympus"),
        ("timezone", "America"),  # a tzdata directory: PermissionError on Windows
        ("currency", "RUPEES"),
        ("locale", "english"),
        ("seat_limit", 0),
        ("admin_email", "not-an-email"),
        ("name", "   "),
    ],
)
async def test_bad_tenant_fields_are_422(op_client: httpx.AsyncClient, field: str, value: object) -> None:
    resp = await op_client.post(TENANTS, json=tenant_body(**{field: value}), headers=csrf(op_client))
    assert resp.status_code == 422, resp.text
    assert [f["field"] for f in resp.json()["error"]["details"]["fields"]] == [f"body.{field}"]


async def test_list_and_get_tenants(op_client: httpx.AsyncClient) -> None:
    created = []
    for _ in range(2):
        r = await op_client.post(TENANTS, json=tenant_body(), headers=csrf(op_client))
        assert r.status_code == 201
        created.append(r.json())

    page = await op_client.get(TENANTS, params={"page_size": 2})
    assert page.status_code == 200
    data = page.json()
    assert set(data) == {"items", "total", "page", "page_size"}
    assert data["total"] >= 2
    assert [t["id"] for t in data["items"]] == [created[1]["id"], created[0]["id"]]  # newest first

    one = await op_client.get(f"{TENANTS}/{created[0]['id']}")
    assert one.status_code == 200
    assert one.json() == created[0]

    missing = await op_client.get(f"{TENANTS}/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_invitation_failure_rolls_back_tenant(
    op_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("invitation insert failed")

    monkeypatch.setattr(service, "create_invitation", boom)
    slug = unique_slug("comp")
    sent_before = len(_outbox().sent)
    resp = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert resp.status_code == 500
    async with platform_session() as db:
        assert (await db.execute(select(Tenant).where(Tenant.slug == slug))).first() is None
    assert len(_outbox().sent) == sent_before


async def test_invitation_token_is_single_use(op_client: httpx.AsyncClient) -> None:
    resp = await op_client.post(TENANTS, json=tenant_body(), headers=csrf(op_client))
    tenant_id = uuid.UUID(resp.json()["id"])
    token = _token_from(_invitation_mail(str(tenant_id)))

    # Without the right tenant context the invitation is invisible (RLS), so it cannot be claimed.
    async with tenant_session(uuid.uuid4()) as db:
        assert await service.claim_invitation(db, token) is None
    async with tenant_session(tenant_id) as db:
        assert await service.claim_invitation(db, token + "x") is None
        claimed = await service.claim_invitation(db, token)
        assert claimed is not None
        assert claimed.accepted_at is not None
    async with tenant_session(tenant_id) as db:
        assert await service.claim_invitation(db, token) is None


async def test_expired_invitation_cannot_be_claimed(op_client: httpx.AsyncClient) -> None:
    resp = await op_client.post(TENANTS, json=tenant_body(), headers=csrf(op_client))
    tenant_id = uuid.UUID(resp.json()["id"])
    token = _token_from(_invitation_mail(str(tenant_id)))
    async with tenant_session(tenant_id) as db:
        await db.execute(update(Invitation).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    async with tenant_session(tenant_id) as db:
        assert await service.claim_invitation(db, token) is None


def test_parse_invitation_token_rejects_malformed() -> None:
    tid = uuid.uuid4()
    assert service.parse_invitation_token(service.invitation_token(tid)) == tid
    for bad in ["", "abc", f"{tid.hex}.", f"{tid.hex}.short", f"{'z' * 32}.{'a' * 43}", "a.b.c"]:
        assert service.parse_invitation_token(bad) is None


async def test_mail_failure_does_not_fail_provisioning(
    op_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenMailer:
        def send(self, message: EmailMessage) -> None:
            raise OSError("smtp down")

    monkeypatch.setattr("app.core.mailer.get_mailer", BrokenMailer)
    resp = await op_client.post(TENANTS, json=tenant_body(), headers=csrf(op_client))
    assert resp.status_code == 201
    assert len(await _invitations(uuid.UUID(resp.json()["id"]))) == 1


async def test_non_slug_integrity_error_is_not_mapped_to_slug_taken(
    op_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    def fake_violation(_: IntegrityError) -> str:
        return "ck_tenants_seat_limit_positive"

    monkeypatch.setattr(service, "violated_constraint", fake_violation)
    slug = unique_slug("other")
    assert (
        await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    ).status_code == 201
    # Same slug again with the pre-check blinded: the INSERT raises, reported as a non-slug constraint.
    original_select = sa.select

    def blind_select(*args: object) -> object:
        stmt = original_select(*args)  # type: ignore[call-overload]
        if args and args[0] is Tenant.id:
            return stmt.where(text("false"))
        return stmt

    monkeypatch.setattr("app.services.auth_service.select", blind_select)
    resp = await op_client.post(TENANTS, json=tenant_body(slug=slug), headers=csrf(op_client))
    assert resp.status_code == 500


async def test_compensation_failure_keeps_original_error(
    operator: OperatorCreds, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: object, **__: object) -> None:
        raise LookupError("original failure")

    def broken_delete(*_: object) -> object:
        raise RuntimeError("compensation failed")

    monkeypatch.setattr(service, "create_invitation", boom)
    monkeypatch.setattr(service, "delete", broken_delete)
    with pytest.raises(LookupError, match="original failure"):
        await service.create_tenant(
            RequestContext(principal=Principal(type="platform", id=operator.id)),
            TenantCreate.model_validate(tenant_body()),
        )
