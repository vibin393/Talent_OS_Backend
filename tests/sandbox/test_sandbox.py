"""Story 66.9: Sandbox Tenant mode, synthetic seed and the enforcement points (FR-339, NFR-68)."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core import mailer as mailer_mod
from app.core.database import platform_session, tenant_session
from app.core.errors import ValidationFailed
from app.core.middleware import sandbox as sandbox_mw
from app.core.settings import get_settings
from app.core.tenancy import clear_sandbox_cache, is_sandbox
from app.models.identity import Tenant
from app.services import imports_service, sandbox_service
from app.services.ai import gateway
from tests.modules.identity.conftest import join, make_tenant

SANDBOXES = "/api/v1/platform/sandboxes"


async def _mark_sandbox(tenant_id: uuid.UUID) -> None:
    async with platform_session() as db:
        await db.execute(
            text("UPDATE tenants SET mode = 'sandbox', sandbox_owner_kind = 'platform' WHERE id = :t"),
            {"t": tenant_id},
        )
    clear_sandbox_cache(tenant_id)


async def _create(op: Any, **body: Any) -> dict[str, Any]:
    payload = {"name": "Dev Sandbox", "owner_kind": "platform", "size": "small", "seed": 42, **body}
    resp = await op.client.post(SANDBOXES, json=payload, headers=op.h())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ================================================================ creation + seed
async def test_create_sandbox_tenant_sets_mode_owner_and_seeds(app: FastAPI, mfa_operator: Any) -> None:
    out = await _create(mfa_operator)
    tid = uuid.UUID(out["tenant"]["id"])
    assert out["mode"] == "sandbox"
    assert out["owner_kind"] == "platform"
    assert out["owner_id"] == str(mfa_operator.id)
    assert out["seed"]["counts"] == sandbox_service.SIZES["small"]
    assert out["seed"]["plan_digest"] == sandbox_service.plan_digest(sandbox_service.build_plan("small", 42))
    async with platform_session() as db:
        row = (await db.execute(select(Tenant).where(Tenant.id == tid))).scalar_one()
        assert (row.mode, row.sandbox_owner_kind, row.rate_tier) == ("sandbox", "platform", "sandbox")
        assert row.sandbox_owner_id == mfa_operator.id
        assert row.sandbox_last_active_at is not None
    assert await is_sandbox(tid)
    async with tenant_session(tid) as db:
        content = await sandbox_service.read_content(db)
        for key, n in sandbox_service.SIZES["small"].items():
            assert len(content[key]) == n, key
        emails, phones = await sandbox_service.synthetic_contact_points(db)
    assert emails
    assert phones
    assert all(sandbox_service.is_reserved_email(e) for e in emails), emails[:3]
    assert all(sandbox_service.is_reserved_phone(p) for p in phones), phones[:3]


async def test_owner_validation(app: FastAPI, mfa_operator: Any) -> None:
    op = mfa_operator
    resp = await op.client.post(
        SANDBOXES, json={"name": "X", "owner_kind": "tenant", "owner_id": str(uuid.uuid4())}, headers=op.h()
    )
    assert resp.status_code == 404
    resp = await op.client.post(SANDBOXES, json={"name": "X", "owner_kind": "partner_org"}, headers=op.h())
    assert resp.status_code == 422
    owner = await make_tenant()
    out = await _create(op, owner_kind="tenant", owner_id=str(owner.id), seed=1)
    assert out["owner_kind"] == "tenant"
    assert out["owner_id"] == str(owner.id)


def test_plan_determinism() -> None:
    a, b = sandbox_service.build_plan("small", 7), sandbox_service.build_plan("small", 7)
    assert sandbox_service.plan_digest(a) == sandbox_service.plan_digest(b)
    assert sandbox_service.plan_digest(a) != sandbox_service.plan_digest(sandbox_service.build_plan("small", 8))
    medium = sandbox_service.build_plan("medium", 7)
    assert medium.counts() == {k: v * 5 for k, v in sandbox_service.SIZES["small"].items()}
    for plan in (a, medium):
        assert all(sandbox_service.is_reserved_email(c["email"]) for c in plan.candidates)
        assert all(sandbox_service.is_reserved_phone(c["phone"]) for c in plan.candidates)
        assert all(sandbox_service.is_reserved_email(c["contact"]["email"]) for c in plan.clients)
        assert all(sandbox_service.is_reserved_phone(c["contact"]["phone"]) for c in plan.clients)
        pairs = {(x["candidate"], x["demand"]) for x in plan.applications}
        assert len(pairs) == len(plan.applications)


def test_reserved_checks() -> None:
    assert sandbox_service.is_reserved_phone("+910000123456")
    assert not sandbox_service.is_reserved_phone("+919876543210")
    assert sandbox_service.is_reserved_email("a.b.1@example.test")
    assert sandbox_service.is_reserved_email("a.b.1@acme.example.test")
    assert not sandbox_service.is_reserved_email("a@example.com")


async def test_seeded_content_is_identical_for_the_same_seed(app: FastAPI, mfa_operator: Any) -> None:
    t1 = uuid.UUID((await _create(mfa_operator, seed=99))["tenant"]["id"])
    t2 = uuid.UUID((await _create(mfa_operator, seed=99))["tenant"]["id"])
    t3 = uuid.UUID((await _create(mfa_operator, seed=100))["tenant"]["id"])
    digests = []
    for tid in (t1, t2, t3):
        async with tenant_session(tid) as db:
            digests.append(await sandbox_service.content_digest(db))
    assert digests[0] == digests[1]
    assert digests[0] != digests[2]


# ================================================================ header + activity
async def test_header_present_for_sandbox_absent_for_production(app: FastAPI) -> None:
    prod = await make_tenant()
    sbx = await make_tenant()
    await _mark_sandbox(sbx.id)
    sandbox_mw.reset_touch_throttle()
    m_prod = await join(app, prod)
    m_sbx = await join(app, sbx)
    try:
        r1 = await m_prod.client.get("/api/v1/auth/me")
        r2 = await m_sbx.client.get("/api/v1/auth/me")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert "x-talentos-sandbox" not in r1.headers
        assert r2.headers.get("x-talentos-sandbox") == "true"
        anon = await m_sbx.client.get("/api/health")
        assert "x-talentos-sandbox" not in anon.headers
    finally:
        await m_prod.client.aclose()
        await m_sbx.client.aclose()
    async with platform_session() as db:
        stamped = (
            await db.execute(select(Tenant.sandbox_last_active_at).where(Tenant.id == sbx.id))
        ).scalar_one()
    assert stamped is not None


async def test_activity_touch_is_throttled(app: FastAPI) -> None:
    sbx = await make_tenant()
    await _mark_sandbox(sbx.id)
    sandbox_mw.reset_touch_throttle()
    assert await sandbox_mw.touch_activity(sbx.id) is True
    assert await sandbox_mw.touch_activity(sbx.id) is False


# ================================================================ is_sandbox cache
async def test_is_sandbox_cache(app: FastAPI) -> None:
    t = await make_tenant()
    clear_sandbox_cache(t.id)
    assert await is_sandbox(t.id) is False
    async with platform_session() as db:
        await db.execute(
            text("UPDATE tenants SET mode = 'sandbox', sandbox_owner_kind = 'platform' WHERE id = :t"),
            {"t": t.id},
        )
    assert await is_sandbox(t.id) is False  # cached for 60 s
    clear_sandbox_cache(t.id)
    assert await is_sandbox(t.id) is True
    assert await is_sandbox(None) is False


# ================================================================ mailer
class _SpyMailer:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, message: Any) -> None:
        self.sent.append(message)


def _msg(tenant_id: uuid.UUID, **extra: Any) -> mailer_mod.EmailMessage:
    return mailer_mod.EmailMessage(to="a@example.com", subject="s", text="t", tenant_id=tenant_id, **extra)


async def test_mailer_uses_outbox_in_sandbox(app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prod = await make_tenant()
    sbx = await make_tenant()
    await _mark_sandbox(sbx.id)
    spy = _SpyMailer()
    monkeypatch.setattr(mailer_mod, "get_mailer", lambda: spy)
    monkeypatch.setattr(get_settings(), "outbox_dir", str(tmp_path))
    await mailer_mod.send_email(_msg(sbx.id))
    assert spy.sent == []
    assert len(list(tmp_path.glob("*.json"))) == 1
    await mailer_mod.send_email(_msg(prod.id))
    assert len(spy.sent) == 1

    called: list[Any] = []

    async def mailbox(*args: Any) -> bool:
        called.append(args)
        return True

    monkeypatch.setattr(mailer_mod, "_mailbox_sender", mailbox)
    assert await mailer_mod.send_routed(_msg(sbx.id, sender_user_id=uuid.uuid4())) == "platform"
    assert called == []
    assert await mailer_mod.send_routed(_msg(prod.id, sender_user_id=uuid.uuid4())) == "mailbox"


# ================================================================ AI
async def test_ai_resolves_to_fake_in_sandbox(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    prod = await make_tenant()
    sbx = await make_tenant()
    await _mark_sandbox(sbx.id)
    gateway.set_client_factory(None)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-not-used")
    assert await gateway.sandbox_client_factory(prod.id) is None
    factory = await gateway.sandbox_client_factory(sbx.id)
    assert factory is not None
    client = factory("sk-not-used")
    assert type(client).__name__ == "SimpleNamespace"  # the keyless Fake LLM, never anthropic.AsyncAnthropic


# ================================================================ import cap
async def test_import_cap(app: FastAPI) -> None:
    prod = await make_tenant()
    sbx = await make_tenant()
    await _mark_sandbox(sbx.id)
    cap = get_settings().sandbox_import_cap
    register = imports_service.register_preview
    async with tenant_session(sbx.id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await register(None, db, "test", None, "a" * 64, cap + 1, 0, {})  # type: ignore[arg-type]
        assert exc.value.code == "sandbox_import_cap"
        ok = await register(None, db, "test", None, "b" * 64, cap, 0, {})  # type: ignore[arg-type]
        assert ok.row_count == cap
    async with tenant_session(prod.id) as db:
        ok = await register(None, db, "test", None, "c" * 64, cap * 5, 0, {})  # type: ignore[arg-type]
        assert ok.row_count == cap * 5
