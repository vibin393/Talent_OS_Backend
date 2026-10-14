"""Fan migrations out across every tenant database — D11 / AD-29.

In the silo model each tenant has its own DB, so a release must migrate them all. This reads the
control-plane registry and runs ``alembic upgrade heads`` against each live tenant's DB (as owner),
then records the new schema version. Stops on the first failure and reports which tenant.

Usage (from Backend/, as the owner role DB_USER):
    ./venv/Scripts/python.exe scripts/migrate_all_tenants.py
    ./venv/Scripts/python.exe scripts/migrate_all_tenants.py --include-platform  # also migrate platform DB
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import tenant_registry  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from scripts.setup_db import migrate  # noqa: E402


async def _load_targets(include_platform: bool) -> list[tuple[str, str]]:
    """Return (label, db_name) pairs to migrate."""
    targets: list[tuple[str, str]] = []
    if include_platform:
        targets.append(("platform", get_settings().platform_db()))
    for rec in await tenant_registry.list_tenants(only_live=True):
        targets.append((rec.slug, rec.db_name))
    return targets


async def _record_version() -> None:
    for rec in await tenant_registry.list_tenants(only_live=True):
        await tenant_registry.set_schema_version(rec.tenant_id, "head")
    await tenant_registry.dispose_control_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--include-platform", action="store_true", help="also migrate the platform DB")
    args = parser.parse_args(argv)

    targets = asyncio.run(_load_targets(args.include_platform))
    if not targets:
        print("[migrate_all_tenants] no tenants registered — nothing to do.")
        return 0

    failures: list[str] = []
    for label, db_name in targets:
        try:
            migrate(db_name)
            print(f"[migrate_all_tenants] {label}: {db_name} -> head")
        except Exception as exc:
            failures.append(f"{label} ({db_name}): {type(exc).__name__}: {exc}")
            print(f"[migrate_all_tenants] FAILED {label}: {db_name}: {exc}", file=sys.stderr)
            break

    if failures:
        print(f"[migrate_all_tenants] stopped after {len(failures)} failure(s).", file=sys.stderr)
        return 1

    asyncio.run(_record_version())
    print(f"[migrate_all_tenants] all {len(targets)} database(s) migrated to head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
