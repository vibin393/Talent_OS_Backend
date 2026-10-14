"""Seed the entitlements catalog + assign tenant plans (Phase 1, Epic 4).

Creates the plan catalog in the platform DB and assigns each registered tenant a plan. With no
--tenant-id it seeds the catalog and defaults every registered tenant to "standard"; with
--tenant-id/--plan it assigns one tenant.

Usage (from Backend/):
    python scripts/seed_entitlements.py                                  # catalog + default all to standard
    python scripts/seed_entitlements.py --tenant-id <uuid> --plan enterprise
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import tenant_registry  # noqa: E402
from app.services import entitlements  # noqa: E402


async def _run(tenant_id: str | None, plan: str) -> None:
    await entitlements.seed_plans()
    print(f"[seed_entitlements] plans seeded: {', '.join(entitlements.PLAN_MODULES)}")
    if tenant_id:
        await entitlements.set_tenant_plan(tenant_id, plan)
        print(f"[seed_entitlements] tenant {tenant_id} -> {plan}")
    else:
        tenants = await tenant_registry.list_tenants(only_live=True)
        for rec in tenants:
            await entitlements.set_tenant_plan(rec.tenant_id, plan)
            print(f"[seed_entitlements] {rec.slug} ({rec.tenant_id}) -> {plan}")
        await tenant_registry.dispose_control_engine()
    await entitlements.dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--plan", default="standard", choices=sorted(entitlements.PLAN_MODULES))
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args.tenant_id, args.plan))
    except Exception as exc:
        print(f"[seed_entitlements] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
