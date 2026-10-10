"""AC: fresh operator via the CLI -> login -> create tenant -> tenant, RLS-scoped invitation, outbox email."""

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

from app.core.database import platform_session, tenant_session
from app.core.security import verify_password
from app.core.settings import get_settings
from app.models.identity import Invitation, PlatformOperator, Tenant

from .conftest import csrf, tenant_body

BACKEND_DIR = Path(__file__).resolve().parents[3]
PASSWORD = "cli password 123"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=BACKEND_DIR,
        env=dict(os.environ),  # DB_NAME / OUTBOX_DIR point at the test DB / temp outbox (conftest)
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


async def test_cli_create_operator_then_provision_tenant(client: httpx.AsyncClient) -> None:
    email = f"cli-{uuid.uuid4().hex[:10]}@example.com"
    proc = _cli("create-operator", "--email", email.upper(), "--name", "CLI Operator", "--password", PASSWORD)
    assert proc.returncode == 0, proc.stderr
    assert "Created platform operator" in proc.stdout
    assert PASSWORD not in proc.stdout + proc.stderr

    async with platform_session() as db:
        op = (await db.execute(select(PlatformOperator).where(PlatformOperator.email == email))).scalar_one()
    assert op.email == email  # stored lower-cased
    assert op.password_hash.startswith("$argon2id$")
    assert verify_password(op.password_hash, PASSWORD)
    assert (op.mfa_enabled, op.mfa_secret_encrypted, op.is_active) == (False, None, True)

    # Idempotent error: second run fails and changes nothing.
    again = _cli("create-operator", "--email", email, "--name", "Other", "--password", "another password")
    assert again.returncode == 1
    assert "operator_exists" in again.stderr
    async with platform_session() as db:
        ops = (await db.execute(select(PlatformOperator).where(PlatformOperator.email == email))).scalars()
        assert [o.name for o in ops] == ["CLI Operator"]

    login = await client.post("/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = tenant_body()
    created = await client.post("/api/v1/platform/tenants", json=body, headers=csrf(client))
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])

    async with platform_session() as db:
        assert (await db.get(Tenant, tenant_id)) is not None
    async with tenant_session(tenant_id) as db:
        (inv,) = (await db.execute(select(Invitation))).scalars().all()
    assert inv.tenant_id == tenant_id

    outbox = Path(get_settings().outbox_dir)
    mails = [json.loads(p.read_text(encoding="utf-8")) for p in outbox.glob("*.json")]
    (mail,) = [m for m in mails if m["tenant_id"] == str(tenant_id)]
    assert mail["to"] == str(body["admin_email"]).lower()
    frontend = get_settings().frontend_url.rstrip("/")
    link = re.escape(frontend) + r"/accept-invite#token=[0-9a-f]{32}\.[A-Za-z0-9_-]{43}"
    assert re.search(link, mail["text"])


def test_cli_rejects_weak_password() -> None:
    proc = _cli(
        "create-operator",
        "--email",
        f"weak-{uuid.uuid4().hex[:8]}@example.com",
        "--name",
        "W",
        "--password",
        "short",
    )
    assert proc.returncode == 1
    assert "weak_password" in proc.stderr


async def test_create_operator_validates_email_and_name_like_the_api() -> None:
    import pytest

    from app.core.errors import ValidationFailed
    from app.services import auth_service as service

    for bad in ["a@b", "not an email", "x@@example.com", ""]:
        with pytest.raises(ValidationFailed) as exc:
            await service.create_operator(bad, "Name", PASSWORD)
        assert exc.value.code == "invalid_email"
    with pytest.raises(ValidationFailed) as exc:
        await service.create_operator(f"n-{uuid.uuid4().hex[:8]}@example.com", "x" * 201, PASSWORD)
    assert exc.value.code == "invalid_name"


def test_cli_rejects_invalid_email_without_traceback() -> None:
    proc = _cli("create-operator", "--email", "a@b", "--name", "N", "--password", PASSWORD)
    assert proc.returncode == 1
    assert "invalid_email" in proc.stderr
    assert "Traceback" not in proc.stderr
