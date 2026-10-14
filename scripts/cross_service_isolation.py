"""Cross-service tenant-isolation suite (Phase 1, Epic 7.2) — the release gate.

Where Epic 7.1 (isolation_suite.py) proves isolation *within* Talent OS, this proves it *across
services*: in the DB-per-tenant model every (service, tenant) pair has its OWN physical database,
so no two tenants — and no two services — ever share storage. It reads the shared control-plane
registry (`tenant_db_map`) and asserts, then connects to each database to confirm it is real,
reachable, and distinct.

Exit code 0 = isolation holds; 1 = a violation (so CI can gate on it). Run against the control DB
(owner role); works regardless of TENANCY_MODE since it reads the registry directly.

Usage (from Backend/):  ./venv/Scripts/python.exe scripts/cross_service_isolation.py
"""

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.settings import get_settings  # noqa: E402


async def _registry_rows() -> list[dict]:
    s = get_settings()
    engine = create_async_engine(s.owner_db_url(s.control_db_name), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text("SELECT service, slug, db_name, status FROM tenant_db_map ORDER BY service, slug")
            )
            return [dict(r._mapping) for r in res]
    finally:
        await engine.dispose()


async def _current_db(db_name: str) -> str | None:
    s = get_settings()
    engine = create_async_engine(s.owner_db_url(db_name), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return str(await conn.scalar(text("SELECT current_database()")))
    except Exception:
        return None
    finally:
        await engine.dispose()


async def main() -> int:
    rows = await _registry_rows()
    if not rows:
        print("cross_service_isolation: control registry empty — provision tenants first.", file=sys.stderr)
        return 2

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 1. Every (service, tenant) pair maps to a DISTINCT database — no shared storage anywhere.
    seen: dict[str, str] = {}
    for r in rows:
        label = f"{r['service']}/{r['slug']}"
        db = r["db_name"]
        if db in seen:
            check(f"{label}: DB {db} not shared with {seen[db]}", False, "shared storage")
        else:
            check(f"{label}: distinct DB ({db})", True)
        seen[db] = label

    # 2. Each registered DB is real, reachable, and reports its own name (physical separation).
    for r in rows:
        label = f"{r['service']}/{r['slug']}"
        actual = await _current_db(r["db_name"])
        check(f"{label}: {r['db_name']} reachable & self-named", actual == r["db_name"],
              f"got {actual}")

    # 3. Cross-service: the same tenant's TOS DB and Croar DB are different physical DBs.
    by_tenant: dict[str, list[str]] = {}
    for r in rows:
        by_tenant.setdefault(r["slug"], []).append(r["db_name"])
    for slug, dbs in by_tenant.items():
        check(f"tenant {slug}: per-service DBs are distinct ({', '.join(dbs)})", len(set(dbs)) == len(dbs))

    passed = sum(1 for _, ok, _ in checks if ok)
    services = sorted({r["service"] for r in rows})
    tenants = sorted({r["slug"] for r in rows})
    print(f"\nCross-service isolation — services={services} tenants={tenants} "
          f"({len(rows)} databases), {passed}/{len(checks)} checks passed\n")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if (detail and not ok) else ''}")
    all_ok = passed == len(checks)
    print("\nRESULT:", "cross-service tenant isolation holds ✓" if all_ok else "ISOLATION VIOLATION ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
