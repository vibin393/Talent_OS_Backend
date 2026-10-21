"""Per-tenant outbound email (SMTP) settings: the admin API (GET/PUT/test), password encryption at rest and
per-tenant mailer resolution (tenant SMTP vs the shared platform mailer)."""

import uuid
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core import mailer as mailer_mod
from app.core.database import tenant_session
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.core.security import decrypt_str

from .conftest import Org, join, make_tenant

MAIL = "/api/v1/admin/mail-settings"
TEST = "/api/v1/admin/mail-settings/test"

SMTP_BODY = {
    "backend": "smtp",
    "smtp_address": "smtp.acme.example.com",
    "smtp_port": 587,
    "smtp_username": "mailer@acme.example.com",
    "smtp_password": "s3cr3t-app-password",
    "smtp_use_ssl": False,
    "sender_email": "hr@acme.example.com",
    "sender_name": "Acme HR",
    "enabled": True,
}


async def _raw_password_cipher(tenant_id: uuid.UUID) -> str | None:
    async with tenant_session(tenant_id) as db:
        return (
            await db.execute(
                text("SELECT smtp_password_encrypted FROM tenant_mail_settings WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------- GET defaults + masking
async def test_get_returns_platform_defaults_and_masks_password(org: Org) -> None:
    resp = await org.admin.client.get(MAIL, headers=org.admin.h())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "platform" and body["enabled"] is False
    assert body["password_set"] is False
    assert "smtp_password" not in body and "smtp_password_encrypted" not in body


# ---------------------------------------------------------------- PUT upsert + encryption at rest
async def test_put_upserts_encrypts_and_masks(org: Org) -> None:
    put = await org.admin.client.put(MAIL, json=SMTP_BODY, headers=org.admin.h())
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["backend"] == "smtp" and body["enabled"] is True
    assert body["smtp_address"] == "smtp.acme.example.com"
    assert body["password_set"] is True
    assert "smtp_password" not in body  # never serialised back

    # Stored ciphertext differs from the plaintext and decrypts back to it (connector purpose).
    cipher = await _raw_password_cipher(org.tenant.id)
    assert cipher is not None and cipher != SMTP_BODY["smtp_password"]
    assert decrypt_str("connector", cipher) == SMTP_BODY["smtp_password"]

    # GET masks it too.
    got = await org.admin.client.get(MAIL, headers=org.admin.h())
    assert got.json()["password_set"] is True and "smtp_password" not in got.json()


async def test_put_omitted_password_keeps_existing(org: Org) -> None:
    await org.admin.client.put(MAIL, json=SMTP_BODY, headers=org.admin.h())
    cipher_before = await _raw_password_cipher(org.tenant.id)

    # Second PUT without smtp_password: config changes, password preserved.
    body2 = {**SMTP_BODY, "sender_name": "Acme People"}
    del body2["smtp_password"]
    resp = await org.admin.client.put(MAIL, json=body2, headers=org.admin.h())
    assert resp.status_code == 200, resp.text
    assert resp.json()["sender_name"] == "Acme People"
    assert resp.json()["password_set"] is True

    cipher_after = await _raw_password_cipher(org.tenant.id)
    assert cipher_after == cipher_before  # unchanged ciphertext
    assert decrypt_str("connector", cipher_after) == SMTP_BODY["smtp_password"]


async def test_put_rejects_smtp_without_address_or_sender(org: Org) -> None:
    bad = {**SMTP_BODY, "smtp_address": ""}
    resp = await org.admin.client.put(MAIL, json=bad, headers=org.admin.h())
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------- mailer resolution
class _FakeSMTP:
    """Captures the connection + login so tests can assert the tenant's SMTP was used."""

    captured: ClassVar[dict[str, Any]] = {}

    def __init__(self, host: str, port: int, timeout: float) -> None:
        _FakeSMTP.captured = {"host": host, "port": port}

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None: ...

    def starttls(self, context: object) -> None:
        _FakeSMTP.captured["tls"] = True

    def login(self, user: str, password: str) -> None:
        _FakeSMTP.captured["login"] = (user, password)

    def send_message(self, msg: object) -> None:
        _FakeSMTP.captured["sent"] = True


async def test_send_uses_tenant_smtp_when_enabled(org: Org, monkeypatch: pytest.MonkeyPatch) -> None:
    await org.admin.client.put(MAIL, json=SMTP_BODY, headers=org.admin.h())
    mailer_mod.invalidate_tenant_mailer(org.tenant.id)
    monkeypatch.setattr(mailer_mod.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.captured = {}

    channel = await mailer_mod.send_email(
        EmailMessage(to="to@example.com", subject="Hi", text="body", tenant_id=org.tenant.id, kind="test")
    )
    assert channel == "tenant_smtp"
    assert _FakeSMTP.captured["host"] == "smtp.acme.example.com"
    assert _FakeSMTP.captured["login"] == ("mailer@acme.example.com", "s3cr3t-app-password")
    assert _FakeSMTP.captured.get("sent") is True


async def test_send_falls_back_to_platform_without_config(app: FastAPI) -> None:
    tenant = await make_tenant()  # no mail settings row
    mailer_mod.invalidate_tenant_mailer(tenant.id)
    platform = get_mailer()
    assert isinstance(platform, OutboxMailer)  # tests force MAILER_BACKEND=outbox
    before = len(platform.sent)

    channel = await mailer_mod.send_email(
        EmailMessage(to="x@example.com", subject="Hi", text="b", tenant_id=tenant.id, kind="test")
    )
    assert channel == "platform"
    assert len(platform.sent) == before + 1


async def test_disabled_tenant_config_falls_back_to_platform(org: Org) -> None:
    body = {**SMTP_BODY, "enabled": False}
    await org.admin.client.put(MAIL, json=body, headers=org.admin.h())
    mailer_mod.invalidate_tenant_mailer(org.tenant.id)
    channel = await mailer_mod.send_email(
        EmailMessage(to="x@example.com", subject="Hi", text="b", tenant_id=org.tenant.id, kind="test")
    )
    assert channel == "platform"


# ---------------------------------------------------------------- test endpoint
async def test_test_endpoint_sends_through_platform_without_config(org: Org) -> None:
    resp = await org.admin.client.post(TEST, json={"to": "ops@example.com"}, headers=org.admin.h())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["channel"] == "platform" and body["error"] is None

    platform = get_mailer()
    assert isinstance(platform, OutboxMailer)
    assert any(m.to == "ops@example.com" and m.kind == "mail_settings_test" for m in platform.sent)


# ---------------------------------------------------------------- authorization
async def test_requires_settings_permission(app: FastAPI) -> None:
    tenant = await make_tenant()
    member = await join(app, tenant, "recruiter", name="Rae Recruiter")  # no settings:* perms
    try:
        got = await member.client.get(MAIL, headers=member.h())
        put = await member.client.put(MAIL, json=SMTP_BODY, headers=member.h())
        assert got.status_code == 403, got.text
        assert put.status_code == 403, put.text
    finally:
        await member.client.aclose()
