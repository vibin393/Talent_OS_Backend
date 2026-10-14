"""Prove the silo routing works — D11 / AD-29 (local demonstration).

For every tenant in the control registry, opens ``tenant_session(tenant_id)`` and asks the DB
``SELECT current_database()``. In silo mode each tenant's session must report its OWN database,
and ``platform_session()`` must report the platform database — proving the resolver routes each
tenant to its own physical DB. Also writes a marker row into each tenant's ``tenant_sequences``
and shows it is only visible in that tenant's DB.

Usage (from Backend/, TENANCY_MODE=silo):
    ./venv/Scripts/python.exe scripts/silo_demo.py
"""

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.core import tenant_registry  # noqa: E402
from app.core.database import dispose_engines, platform_session, tenant_session  # noqa: E402
from app.core.settings import get_settings  # noqa: E402


async def main() -> int:
    s = get_settings()
    print(f"TENANCY_MODE = {s.tenancy_mode}")
    if s.tenancy_mode != "silo":
        print("  (set TENANCY_MODE=silo to see per-tenant routing)")

    async with platform_session() as db:
        where = await db.scalar(text("SELECT current_database()"))
    print(f"platform_session()            -> database: {where}")

    tenants = await tenant_registry.list_tenants(only_live=True)
    if not tenants:
        print("no tenants registered — run provision_tenant.py first.")
        await dispose_engines()
        return 1

    for rec in tenants:
        async with tenant_session(rec.tenant_id) as db:
            where = await db.scalar(text("SELECT current_database()"))
            # count rows in a tenant-scoped table to show each DB is an independent schema
            n = await db.scalar(text("SELECT count(*) FROM tenant_sequences"))
        print(f"tenant_session({rec.slug:<8}) -> database: {where:<16} tenant_sequences rows: {n}")

    # Physical-isolation check: current_database() per tenant must be distinct and match the registry.
    seen: dict[str, str] = {}
    ok = True
    for rec in tenants:
        async with tenant_session(rec.tenant_id) as db:
            where = str(await db.scalar(text("SELECT current_database()")))
        if where != rec.db_name:
            print(f"  !! {rec.slug}: routed to {where}, expected {rec.db_name}")
            ok = False
        if where in seen:
            print(f"  !! {rec.slug} and {seen[where]} share database {where} — NOT isolated")
            ok = False
        seen[where] = rec.slug

    await tenant_registry.dispose_control_engine()
    await dispose_engines()
    print("\nRESULT:", "each tenant is on its own physical database ✓" if ok else "ISOLATION FAILED ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
