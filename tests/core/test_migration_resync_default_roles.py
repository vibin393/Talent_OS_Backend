# ruff: noqa: E501
"""Migration 20261001_901: re-syncs SYSTEM non-admin default roles to the frozen Phase 1 DEFAULT_ROLES.

Runs the migration's ``upgrade()`` inside an owner-role transaction that is rolled back afterwards, so the
shared test database is left untouched.
"""

import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from app.core.permissions import DEFAULT_ROLES, TENANT_ADMIN
from tests.factories import create_tenant_row

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "20261001_901_resync_default_roles.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_20261001_901", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_roles() -> dict[str, dict[str, str]]:
    return {
        r.key: {k: v.value for k, v in r.permissions.items()} for r in DEFAULT_ROLES if r.key != TENANT_ADMIN
    }


def test_frozen_snapshot_matches_current_default_roles() -> None:
    """The P1 snapshot is history: Phase 2 only *adds* grants to the P1 roles (AD-16), never removes or
    narrows one. The phase-close merge (``*_900_merge_phase2``) freezes the full current definitions."""
    frozen = _load_migration().FROZEN_DEFAULT_ROLES
    current = _current_roles()
    assert TENANT_ADMIN not in frozen
    changed = {
        role: sorted(k for k, scope in perms.items() if current.get(role, {}).get(k) != scope)
        for role, perms in frozen.items()
    }
    assert {role: keys for role, keys in changed.items() if keys} == {}


def _load_merge(pattern: str) -> ModuleType | None:
    merges = sorted(MIGRATION.parent.glob(pattern))
    if not merges:
        return None
    spec = importlib.util.spec_from_file_location(merges[-1].stem, merges[-1])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase_close_snapshot_matches_current_default_roles() -> None:
    """Coordinator decision 17: the latest phase-close merge (Phase 4: ``*_900_merge_phase4``) freezes the current
    ``DEFAULT_ROLES`` exactly; Phase 3 epics added grants without a resync until that merge."""
    module = (
        _load_merge("*_900_merge_phase5.py")
        or _load_merge("*_900_merge_phase4.py")
        or _load_merge("*_900_merge_phase3.py")
        or _load_merge("*_900_merge_phase2.py")
    )
    if module is None:
        pytest.skip("phase-close merge revision not created yet")
    assert _current_roles() == module.FROZEN_DEFAULT_ROLES
    assert TENANT_ADMIN not in module.FROZEN_DEFAULT_ROLES
    assert set(module.ROLE_LABELS) == set(module.FROZEN_DEFAULT_ROLES)


def test_phase2_snapshot_is_history() -> None:
    """Phase 3 only adds roles / grants: every Phase 2 frozen role still exists in the Phase 3 snapshot."""
    p2, p3 = _load_merge("*_900_merge_phase2.py"), _load_merge("*_900_merge_phase3.py")
    if p2 is None or p3 is None:
        pytest.skip("phase-close merges not both present")
    assert set(p2.FROZEN_DEFAULT_ROLES) <= set(p3.FROZEN_DEFAULT_ROLES)
    assert {"recruitment_marketer", "campus_recruiter", "immigration_coordinator"} <= set(
        p3.FROZEN_DEFAULT_ROLES
    )
    assert p3.FROZEN_DEFAULT_ROLES["account_manager"].get("client_sso:manage")


async def test_phase3_merge_resyncs_and_inserts_system_roles(owner_db: AsyncEngine) -> None:
    migration = _load_merge("*_900_merge_phase3.py")
    if migration is None:
        pytest.skip("phase-3 merge not created yet")
    tenant_id = await create_tenant_row("Phase 3 resync tenant")
    stale = {"candidate:read": "own"}
    async with owner_db.connect() as conn:
        trans = await conn.begin()
        try:
            await _insert_role(conn, tenant_id, TENANT_ADMIN, system=True, perms=stale)
            await _insert_role(conn, tenant_id, "account_manager", system=True, perms=stale)
            await _insert_role(conn, tenant_id, "custom_marketer", system=False, perms=stale)

            def _run(sync_conn: Connection) -> None:
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)
            rows = await conn.execute(
                text("SELECT key, permissions, is_system FROM roles WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            stored = {k: (p, sys_) for k, p, sys_ in rows.tuples().all()}
        finally:
            await trans.rollback()
    frozen = migration.FROZEN_DEFAULT_ROLES
    assert stored["account_manager"][0] == frozen["account_manager"]
    for key in ("recruitment_marketer", "campus_recruiter", "immigration_coordinator"):
        assert stored[key] == (frozen[key], True), key  # missing system roles inserted
    assert stored[TENANT_ADMIN][0] == stale and stored["custom_marketer"][0] == stale


async def _insert_role(
    conn: AsyncConnection, tenant_id: uuid.UUID, key: str, *, system: bool, perms: dict[str, str]
) -> None:
    await conn.execute(
        text(
            "INSERT INTO roles (id, tenant_id, key, name, description, is_system, permissions) "
            "VALUES (gen_random_uuid(), :tid, :key, :key, '', :sys, CAST(:perms AS jsonb))"
        ),
        {"tid": tenant_id, "key": key, "sys": system, "perms": json.dumps(perms)},
    )


async def test_upgrade_resyncs_system_roles_only(owner_db: AsyncEngine) -> None:
    migration = _load_migration()
    tenant_id = await create_tenant_row("Role resync tenant")
    stale = {"candidate:read": "own"}

    async with owner_db.connect() as conn:
        trans = await conn.begin()
        try:
            await _insert_role(conn, tenant_id, TENANT_ADMIN, system=True, perms=stale)
            await _insert_role(conn, tenant_id, "recruiter", system=True, perms=stale)
            await _insert_role(conn, tenant_id, "viewer", system=True, perms=stale)
            await _insert_role(conn, tenant_id, "custom_sourcer", system=False, perms=stale)

            def _run(sync_conn: Connection) -> None:
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)

            rows = await conn.execute(
                text("SELECT key, permissions FROM roles WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            stored = dict(rows.tuples().all())
        finally:
            await trans.rollback()

    frozen = migration.FROZEN_DEFAULT_ROLES
    assert stored["recruiter"] == frozen["recruiter"]
    assert stored["viewer"] == frozen["viewer"]
    assert stored[TENANT_ADMIN] == stale  # Tenant Admin never touched
    assert stored["custom_sourcer"] == stale  # custom roles never touched
    assert "note:write" in stored["recruiter"]


# ---------------------------------------------------------------- Epic 65: Phase 4 close
def test_phase3_snapshot_is_history() -> None:
    """Phase 4 only adds roles / grants: every Phase 3 frozen role and grant survives in the Phase 4 snapshot."""
    p3, p4 = _load_merge("*_900_merge_phase3.py"), _load_merge("*_900_merge_phase4.py")
    if p3 is None or p4 is None:
        pytest.skip("phase-close merges not both present")
    assert set(p3.FROZEN_DEFAULT_ROLES) <= set(p4.FROZEN_DEFAULT_ROLES)
    for role, perms in p3.FROZEN_DEFAULT_ROLES.items():
        assert set(perms) <= set(p4.FROZEN_DEFAULT_ROLES[role]), role
    f = p4.FROZEN_DEFAULT_ROLES
    assert f["ai_administrator"]["ai:govern"] == "tenant" and "candidate:read" not in f["ai_administrator"]
    assert "ai:payload_read" not in f["ai_administrator"] and "proposal:decide" not in f["ai_administrator"]
    assert f["recruiter"]["proposal:decide"] == "team" and f["recruiter"]["copilot:use"] == "tenant"
    assert f["campus_recruiter"]["proposal:decide"] == "team"
    for role in ("finance", "finance_admin"):
        assert f[role]["billing:prepare"] == "tenant" and f[role]["message:send_templated"] == "tenant"
    for role in ("viewer", "grievance_officer", "interviewer"):
        assert "copilot:use" not in f[role] and "proposal:decide" not in f[role]


async def test_phase4_merge_resyncs_and_inserts_ai_administrator(owner_db: AsyncEngine) -> None:
    migration = _load_merge("*_900_merge_phase4.py")
    if migration is None:
        pytest.skip("phase-4 merge not created yet")
    tenant_id = await create_tenant_row("Phase 4 resync tenant")
    stale = {"candidate:read": "own"}
    async with owner_db.connect() as conn:
        trans = await conn.begin()
        try:
            await _insert_role(conn, tenant_id, TENANT_ADMIN, system=True, perms=stale)
            await _insert_role(conn, tenant_id, "recruiter", system=True, perms=stale)
            await _insert_role(conn, tenant_id, "custom_ai", system=False, perms=stale)

            def _run(sync_conn: Connection) -> None:
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)
            rows = await conn.execute(
                text("SELECT key, permissions, is_system FROM roles WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            stored = {k: (p, sys_) for k, p, sys_ in rows.tuples().all()}
        finally:
            await trans.rollback()
    frozen = migration.FROZEN_DEFAULT_ROLES
    # Phase 4's snapshot is history once Phase 5 adds grants: every Phase 4 role survives into the current roles.
    assert set(frozen) <= set(_current_roles())
    assert stored["recruiter"][0] == frozen["recruiter"]
    assert stored["ai_administrator"] == (frozen["ai_administrator"], True)  # inserted
    assert stored[TENANT_ADMIN][0] == stale and stored["custom_ai"][0] == stale


def test_phase4_snapshot_is_history() -> None:
    """Phase 5 only adds roles / grants: every Phase 4 frozen role and grant survives in the Phase 5 snapshot."""
    p4, p5 = _load_merge("*_900_merge_phase4.py"), _load_merge("*_900_merge_phase5.py")
    if p4 is None or p5 is None:
        pytest.skip("phase-close merges not both present")
    assert set(p4.FROZEN_DEFAULT_ROLES) <= set(p5.FROZEN_DEFAULT_ROLES)
    for role, perms in p4.FROZEN_DEFAULT_ROLES.items():
        assert set(perms) <= set(p5.FROZEN_DEFAULT_ROLES[role]), role
    f = p5.FROZEN_DEFAULT_ROLES
    # New Phase 5 grants and the new Integration Administrator system role.
    assert f["recruiter"].get("posting:publish_external") == "team"
    assert f["recruiter"].get("voice:call") == "team" and f["recruiter"].get("mcp:use") == "tenant"
    assert f["recruitment_marketer"].get("campaign:copy_approve") == "tenant"
    assert f["finance_admin"].get("ledger:sync") == "tenant"
    assert "integration_administrator" in f and f["integration_administrator"]["webhook:manage"] == "tenant"
    assert "candidate:read" not in f["integration_administrator"] and "mcp:use" not in f["integration_administrator"]


async def test_phase5_merge_resyncs_and_inserts_integration_administrator(owner_db: AsyncEngine) -> None:
    migration = _load_merge("*_900_merge_phase5.py")
    if migration is None:
        pytest.skip("phase-5 merge not created yet")
    tenant_id = await create_tenant_row("Phase 5 resync tenant")
    stale = {"candidate:read": "own"}
    async with owner_db.connect() as conn:
        trans = await conn.begin()
        try:
            await _insert_role(conn, tenant_id, TENANT_ADMIN, system=True, perms=stale)
            await _insert_role(conn, tenant_id, "recruiter", system=True, perms=stale)
            await _insert_role(conn, tenant_id, "custom_int", system=False, perms=stale)

            def _run(sync_conn: Connection) -> None:
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)
            rows = await conn.execute(
                text("SELECT key, permissions, is_system FROM roles WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            stored = {k: (p, sys_) for k, p, sys_ in rows.tuples().all()}
        finally:
            await trans.rollback()
    frozen = migration.FROZEN_DEFAULT_ROLES
    assert frozen == _current_roles()
    assert stored["recruiter"][0] == frozen["recruiter"]
    assert stored["integration_administrator"] == (frozen["integration_administrator"], True)  # inserted
    assert stored[TENANT_ADMIN][0] == stale and stored["custom_int"][0] == stale
