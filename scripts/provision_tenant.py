"""Provision a tenant in the DB-per-tenant (silo) model — D11 / AD-29.

Creates the tenant's own database, migrates the full Talent OS schema into it (which also
creates/asserts the ``talent_app`` runtime role and its grants + RLS, via the baseline migration),
and registers the tenant in the control-plane registry so the request path can route to it.

With ``--admin-email`` it also SEEDS a usable tenant: the ``Tenant`` row in the platform DB, the
default roles + a ``tenant_admin`` admin ``User`` (with a known password) in the tenant's own DB —
so the tenant can sign in immediately.

Usage (from Backend/, as the owner role DB_USER, with TENANCY_MODE=silo in the environment):
    ./venv/Scripts/python.exe scripts/provision_tenant.py --slug acme
    ./venv/Scripts/python.exe scripts/provision_tenant.py --slug acme \
        --admin-email admin@acme.test --admin-password 'ChangeMe!2026' --name "Acme Corp"

Idempotent: re-running re-asserts the DB, migrations (to head), registry row, Tenant row and admin.
"""

import argparse
import asyncio
import datetime
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import tenant_registry  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from scripts.setup_db import ensure_database, migrate  # noqa: E402 — reuse the owner-role helpers

# India-launch defaults for a seeded tenant (overridable via flags).
DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_CURRENCY = "INR"
DEFAULT_LOCALE = "en-IN"
DEFAULT_SEAT_LIMIT = 25


async def _register(tenant_id: uuid.UUID, slug: str, db_name: str, cell: str) -> None:
    await tenant_registry.ensure_registry_schema()
    await tenant_registry.register_tenant(
        tenant_id, slug, db_name, cell=cell, schema_version="head", status="active"
    )
    await tenant_registry.dispose_control_engine()


async def _seed_tenant_and_admin(
    tenant_id: uuid.UUID,
    slug: str,
    name: str,
    admin_email: str,
    admin_password: str,
    *,
    timezone: str,
    currency: str,
    locale: str,
    seat_limit: int,
) -> None:
    """Seed the platform ``Tenant`` row + the tenant DB's default roles and admin user."""
    from sqlalchemy import select

    from app.core import audit
    from app.core.database import dispose_engines, platform_session, tenant_session
    from app.core.security import hash_password
    from app.models.identity import Tenant, User
    from app.services import access_service as access

    def _tenant_kwargs() -> dict:
        return {
            "id": tenant_id, "name": name, "slug": slug, "status": "active",
            "timezone": timezone, "currency": currency, "locale": locale, "seat_limit": seat_limit,
        }

    # (a) Tenant row in the PLATFORM DB (login reads tenants + sessions here).
    async with platform_session() as db:
        if await db.get(Tenant, tenant_id) is None:
            db.add(Tenant(**_tenant_kwargs()))

    # (b) Default roles + admin user in the TENANT's OWN DB (login reads users + roles here).
    #     The tenant's own `tenants` row must also exist here: roles/users FK-reference the LOCAL
    #     tenants table (silo — each DB has its own schema), so it is a required self-row, not data.
    async with tenant_session(tenant_id) as tdb:
        if await tdb.get(Tenant, tenant_id) is None:
            tdb.add(Tenant(**_tenant_kwargs()))
            await tdb.flush()
        role_ids = await access.seed_default_roles(tdb, tenant_id)
        existing = (await tdb.execute(select(User.id).where(User.email == admin_email))).first()
        if existing is None:
            user = User(
                id=uuid.uuid4(), tenant_id=tenant_id, email=admin_email, name=name + " Admin",
                password_hash=hash_password(admin_password), is_active=True,
                last_login_at=datetime.datetime.now(tz=datetime.UTC),
            )
            audit.set_actor(tdb, "user", user.id)  # self-signup attribution (as the real flow does)
            tdb.add(user)
            await tdb.flush()
            await access.set_user_roles(tdb, tenant_id, user.id, [role_ids["tenant_admin"]])

    await tenant_registry.dispose_control_engine()
    await dispose_engines()


def provision_tenant(slug: str, tenant_id: uuid.UUID, cell: str) -> str:
    s = get_settings()
    db_name = s.tenant_db_name(slug)
    created = ensure_database(db_name)  # CREATE DATABASE tenant_<slug> if missing
    migrate(db_name)  # full schema + talent_app role + grants + RLS (runs as owner)
    asyncio.run(_register(tenant_id, slug, db_name, cell))
    print(f"[provision_tenant] {slug}: db={db_name} ({'created' if created else 'existing'}), "
          f"migrated, registered tenant_id={tenant_id} cell={cell}")
    return db_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slug", required=True, help="tenant slug (also the local DB suffix)")
    parser.add_argument("--tenant-id", default=None, help="tenant UUID (generated if omitted)")
    parser.add_argument("--cell", default="local", help="cell/region label")
    parser.add_argument("--admin-email", default=None, help="seed a usable tenant with this admin")
    parser.add_argument("--admin-password", default="ChangeMe!2026", help="admin password (dev default)")
    parser.add_argument("--name", default=None, help="tenant display name (default: slug)")
    parser.add_argument("--seat-limit", type=int, default=DEFAULT_SEAT_LIMIT)
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--currency", default=DEFAULT_CURRENCY)
    parser.add_argument("--locale", default=DEFAULT_LOCALE)
    args = parser.parse_args(argv)

    if get_settings().tenancy_mode != "silo":
        print("[provision_tenant] note: TENANCY_MODE is not 'silo' — provisioning anyway, but the "
              "app will not route to this DB until TENANCY_MODE=silo.", file=sys.stderr)
    tenant_id = uuid.UUID(args.tenant_id) if args.tenant_id else uuid.uuid4()
    try:
        provision_tenant(args.slug, tenant_id, args.cell)
        if args.admin_email:
            name = args.name or args.slug
            asyncio.run(
                _seed_tenant_and_admin(
                    tenant_id, args.slug, name, args.admin_email, args.admin_password,
                    timezone=args.timezone, currency=args.currency,
                    locale=args.locale, seat_limit=args.seat_limit,
                )
            )
            print(f"[provision_tenant] {args.slug}: seeded Tenant row + admin {args.admin_email} "
                  f"(tenant_admin). Sign in once the frontend points at this tenant.")
    except Exception as exc:
        print(f"[provision_tenant] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
