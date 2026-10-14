"""Cross-tenant isolation suite (Phase 1, Epic 7.1) — proves tenant A cannot see tenant B.

In the DB-per-tenant (silo) model isolation is physical, so this asserts the guarantee that
matters: a session opened for tenant A reaches only tenant A's database and sees none of tenant B's
rows, and vice versa. It checks two layers:

  1. DATA layer — `tenant_session(A)` sees A's users/roles only; B's admin email is absent from A.
  2. ROUTING layer — each tenant's session reports a distinct `current_database()` matching the
     control registry (no two tenants share a DB).

Exit code 0 = isolation holds; 1 = a violation (so it can gate CI). Needs ≥2 registered tenants
each with a seeded admin (see provision_tenant.py --admin-email). Run with TENANCY_MODE=silo.

Usage (from Backend/):  ./venv/Scripts/python.exe scripts/isolation_suite.py
"""

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select, text  # noqa: E402

from app.core import tenant_registry  # noqa: E402
from app.core.database import dispose_engines, tenant_session  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.models.identity import User  # noqa: E402


class _Fail(Exception):
    pass


async def _tenant_facts(tenant_id) -> dict:
    """What tenant A's own session sees: its database + the set of its user emails."""
    async with tenant_session(tenant_id) as db:
        database = str(await db.scalar(text("SELECT current_database()")))
        emails = set((await db.execute(select(User.email))).scalars().all())
        count = int(await db.scalar(select(func.count()).select_from(User)) or 0)
    return {"database": database, "emails": emails, "count": count}


async def main() -> int:
    s = get_settings()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    if s.tenancy_mode != "silo":
        print(f"isolation_suite: TENANCY_MODE={s.tenancy_mode} (expected 'silo') — aborting.", file=sys.stderr)
        return 2

    tenants = await tenant_registry.list_tenants(only_live=True)
    if len(tenants) < 2:
        print("isolation_suite: need ≥2 registered tenants with seeded admins.", file=sys.stderr)
        await tenant_registry.dispose_control_engine()
        await dispose_engines()
        return 2

    facts = {t.slug: await _tenant_facts(t.tenant_id) for t in tenants}

    # 1. ROUTING — every tenant on its own distinct database, matching the registry.
    by_db: dict[str, str] = {}
    for t in tenants:
        f = facts[t.slug]
        check(f"{t.slug}: routed to its registry DB ({t.db_name})", f["database"] == t.db_name,
              f"got {f['database']}")
        if f["database"] in by_db:
            check(f"{t.slug}: does not share a DB with {by_db[f['database']]}", False, f["database"])
        by_db[f["database"]] = t.slug

    # 2. DATA — no tenant's user emails leak into any other tenant's view.
    for a in tenants:
        for b in tenants:
            if a.slug == b.slug:
                continue
            leaked = facts[a.slug]["emails"] & facts[b.slug]["emails"]
            check(f"{a.slug} sees none of {b.slug}'s users", not leaked, f"leaked: {leaked}")

    await tenant_registry.dispose_control_engine()
    await dispose_engines()

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\nCross-tenant isolation suite — {len(tenants)} tenants, {passed}/{len(checks)} checks passed\n")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if (detail and not ok) else ''}")
    all_ok = passed == len(checks)
    print("\nRESULT:", "tenant isolation holds ✓" if all_ok else "ISOLATION VIOLATION ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
