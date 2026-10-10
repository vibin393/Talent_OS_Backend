"""Story 22.9 (A10): Phase 1 hardening prerequisites -- session purge for external principals, fragment tokens
and the log redactor, per-IP sign-in limits for Client / Vendor portals, the audit purge CLI, the draft-resume
beat, ``talent_app`` provisioning outside Alembic."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.logger import redact
from app.core.storage import new_key
from app.models.candidates import CandidateResume
from app.models.files import StoredFile
from app.models.identity import Session as AuthSession
from app.services import auth_service
from app.services.external_auth import signin
from tests.modules.identity.conftest import make_tenant


# ---------------------------------------------------------------- session purge: client / vendor sessions
async def test_purge_sessions_covers_client_and_vendor_sessions() -> None:
    from sqlalchemy import update

    from app.tasks.maintenance import purge_sessions_job

    tenant = await make_tenant()
    ids: list[uuid.UUID] = []
    async with platform_session() as db:
        for kind in ("client_user", "vendor_user", "client_user"):
            issued = await auth_service.create_session(
                db,
                principal_type=kind,  # type: ignore[arg-type]
                principal_id=uuid.uuid4(),
                tenant_id=tenant.id,
            )
            ids.append(issued.session.id)
        now = datetime.now(UTC)
        live_id = ids.pop()
        await db.execute(
            update(AuthSession).where(AuthSession.id == ids[0]).values(expires_at=now - timedelta(minutes=1))
        )
        await db.execute(
            update(AuthSession).where(AuthSession.id == ids[1]).values(revoked_at=now - timedelta(days=8))
        )
    assert await purge_sessions_job() >= 2
    async with platform_session() as db:
        left = set(
            (await db.execute(select(AuthSession.id).where(AuthSession.id.in_([*ids, live_id])))).scalars()
        )
    assert left == {live_id}


# ---------------------------------------------------------------- magic-link fragments + log redactor
def test_client_and_vendor_links_carry_the_token_in_the_fragment() -> None:
    token = "S3cr3t_Token-abcdefghijklmnop"
    for url in (
        signin.client_sign_in_url("acme", token),
        signin.vendor_invite_url("acme", token),
        signin.vendor_reset_url("acme", token),
    ):
        path, _, fragment = url.partition("#")
        assert token not in path and fragment == f"t={token}"
        assert token not in redact(f"GET {url} 200")
    for url in (
        auth_service.reset_password_url(token),
        f"https://app.example.com/client/auth#token={token}",
        f"https://app.example.com/client/auth#t={token}",
    ):
        assert token not in redact(f"visited {url}")


# ---------------------------------------------------------------- per-IP limits on Client / Vendor sign-in
class _LimitedError(Exception):
    pass


async def test_external_sign_in_paths_enforce_a_per_ip_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import BackgroundTasks

    from app.schemas.external_auth_portal import PortalLinkRequestIn, PortalPasswordLoginIn

    seen: list[tuple[str, str, int]] = []

    async def enforce(policy: str, subject: str, limit: int, window: int) -> Any:
        seen.append((policy, subject, limit))
        raise _LimitedError(policy)

    monkeypatch.setattr(signin.rate_limit, "enforce", enforce)
    ctx = RequestContext(ip="203.0.113.7")
    link = PortalLinkRequestIn(tenant_slug="acme", email="c@example.com")
    login = PortalPasswordLoginIn(tenant_slug="acme", email="v@example.com", password="x")
    with pytest.raises(_LimitedError):
        await signin.request_client_link(ctx, link, BackgroundTasks())
    with pytest.raises(_LimitedError):
        await signin.vendor_login(ctx, login)
    ip_key = signin._ip_key(ctx)
    assert seen == [("client_link_ip", ip_key, signin.IP_LIMIT), ("vendor_login_ip", ip_key, signin.IP_LIMIT)]
    assert 0 < signin.IP_LIMIT <= 30 and signin.WINDOW == 3600


# ---------------------------------------------------------------- audit purge (owner role)
async def _audit(owner_db: AsyncEngine, tenant_id: uuid.UUID, entity_type: str, at: datetime) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with owner_db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_logs (id, tenant_id, occurred_at, actor_type, action, entity_type, "
                "entity_id) VALUES (:id, :t, :at, 'system', 'x.test', :et, :eid)"
            ),
            {"id": row_id, "t": tenant_id, "at": at, "et": entity_type, "eid": str(uuid.uuid4())},
        )
    return row_id


async def test_purge_audit_honours_years_and_refuses_retention_bound_rows(owner_db: AsyncEngine) -> None:
    from app.services.privacy.audit_purge import protected_entity_types, purge_audit

    protected = sorted(protected_entity_types())
    assert protected, "retention bindings are registered by the domain hooks"
    tenant = await make_tenant()
    now = datetime.now(UTC)
    async with owner_db.begin() as conn:
        await conn.execute(
            text("INSERT INTO retention_policies (tenant_id, audit_retention_years) VALUES (:t, 2)"),
            {"t": tenant.id},
        )
    old = await _audit(owner_db, tenant.id, "candidate", now - timedelta(days=3 * 366))
    kept_bound = await _audit(owner_db, tenant.id, protected[0], now - timedelta(days=3 * 366))
    recent = await _audit(owner_db, tenant.id, "candidate", now - timedelta(days=365))

    dry = await purge_audit(owner_db, tenant.id, dry_run=True)
    assert dry["years"] == 2 and dry["deleted"] == 1 and dry["protected"] == 1
    out = await purge_audit(owner_db, tenant.id)
    assert out["deleted"] == 1 and out["protected"] == 1
    async with owner_db.connect() as conn:
        left = set(
            (
                await conn.execute(text("SELECT id FROM audit_logs WHERE tenant_id = :t"), {"t": tenant.id})
            ).scalars()
        )
    assert old not in left and {kept_bound, recent} <= left


def test_purge_audit_cli_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    from app import cli

    assert cli.main(["purge-audit", "--tenant", str(uuid.uuid4()), "--dry-run"]) == 0
    assert "Would delete 0 audit row(s)" in capsys.readouterr().out
    assert cli.main(["purge-audit", "--tenant", "not-a-uuid"]) == 1


# ---------------------------------------------------------------- draft-resume beat
def test_purge_draft_resumes_is_scheduled() -> None:
    from app.core.celery import celery_app
    from app.job.schedules import BEAT_SCHEDULE

    entry = BEAT_SCHEDULE["candidates-purge-draft-resumes"]
    assert entry["task"] == "candidates.purge_draft_resumes"
    assert "candidates.purge_draft_resumes" in celery_app.tasks


async def test_purge_draft_resumes_deletes_drafts_older_than_7_days() -> None:
    from app.tasks.maintenance import purge_draft_resumes_job

    tenant = await make_tenant()
    now = datetime.now(UTC)
    ids: dict[str, uuid.UUID] = {}
    files: dict[str, uuid.UUID] = {}
    async with tenant_session(tenant.id) as db:
        for label, age in (("old", 8), ("fresh", 2)):
            f = StoredFile(
                id=uuid.uuid4(), tenant_id=tenant.id, storage_key=new_key(tenant.id),
                original_name="cv.pdf", content_type="application/pdf", size_bytes=10,
                sha256=hashlib.sha256(label.encode()).hexdigest(), scan_status="skipped_dev",
            )  # fmt: skip
            db.add(f)
            await db.flush()
            r = CandidateResume(
                id=uuid.uuid4(), tenant_id=tenant.id, candidate_id=None, file_id=f.id, text_status="ok",
                parse_status="unavailable", created_at=now - timedelta(days=age),
                updated_at=now - timedelta(days=age),
            )  # fmt: skip
            db.add(r)
            await db.flush()
            ids[label], files[label] = r.id, f.id
    out = await purge_draft_resumes_job(only=str(tenant.id))
    assert out == {str(tenant.id): 1}
    async with tenant_session(tenant.id) as db:
        assert await db.get(CandidateResume, ids["old"]) is None
        assert await db.get(CandidateResume, ids["fresh"]) is not None
        old_file = await db.get(StoredFile, files["old"])
        assert old_file is not None and old_file.deleted_at is not None


# ---------------------------------------------------------------- talent_app provisioning outside Alembic
def test_create_app_role_refuses_in_production_without_a_provisioned_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import migration_helpers as mh

    executed: list[str] = []
    monkeypatch.setattr(mh.op, "execute", lambda sql: executed.append(sql), raising=False)
    monkeypatch.setattr(mh, "_is_production", lambda: True)
    monkeypatch.setattr(mh, "_role_exists", lambda role: False)
    with pytest.raises(RuntimeError, match="provision_app_role"):
        mh.create_app_role("talent_app", "plaintext-pw")
    monkeypatch.setattr(mh, "_role_exists", lambda role: True)
    mh.create_app_role("talent_app", "plaintext-pw")  # provisioned: no DDL, the password is never re-asserted
    assert executed == []
    monkeypatch.setattr(mh, "_is_production", lambda: False)
    mh.create_app_role("talent_app", "dev-pw")
    assert len(executed) == 1 and "CREATE ROLE talent_app" in executed[0]


def test_provision_script_sends_only_a_scram_verifier() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "provision_app_role.py"
    spec = importlib.util.spec_from_file_location("provision_app_role", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # RFC 7677 test vector (user "user", password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096 iterations)
    import base64

    verifier = mod.scram_verifier("pencil", salt=base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ=="))
    assert verifier.startswith("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$")
    sql = mod.role_sql("talent_app", mod.scram_verifier("plaintext-secret"))
    assert "plaintext-secret" not in sql and "SCRAM-SHA-256$4096:" in sql and "NOBYPASSRLS" in sql
    assert mod.main(["--password-env", "E35_UNSET_PASSWORD_VAR"]) == 1
