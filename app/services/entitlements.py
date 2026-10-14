"""Entitlements service (Phase 1, Epic 4): plan -> modules -> features.

A tenant's plan decides which modules it may use. The catalog (plans -> modules) and each tenant's
assignment live in the PLATFORM database (keyed by tenant_id), so there is no per-tenant-DB migration
to fan out — the check is a cell-local read (AD-9). Credits/metering stay in Croar as the usage
sub-system; this service answers only "does this tenant have this module/feature".

Enforcement: `require_module(module)` (API/module boundary) and `GET /api/v1/me/entitlements`
(drives nav). Seeded from the canonical module taxonomy below; per-tenant plans assigned at
provisioning (default "standard").
"""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import get_settings

# Canonical unified module taxonomy (the 9 platform modules + first-party add-ons). Mirrors the
# architecture's module map and the permission-catalog groups; Croar's ModuleScope maps onto these.
MODULES: tuple[str, ...] = (
    "sales",
    "talent_acquisition",
    "vendor_mgmt",
    "contracts",
    "workforce",
    "time_expense",
    "finance",
    "experience",
    "intelligence",
    "assessments",
    "surveys",
    "onboarding",
    "analytics",
    "payroll",
    "ai_copilot",
)

# plan -> the modules it grants.
PLAN_MODULES: dict[str, set[str]] = {
    "sandbox": {"talent_acquisition", "experience"},
    "standard": {
        "sales", "talent_acquisition", "vendor_mgmt", "workforce", "finance",
        "experience", "intelligence", "assessments", "surveys", "onboarding", "analytics",
    },
    "enterprise": set(MODULES),  # everything
}
DEFAULT_PLAN = "standard"

_engine: AsyncEngine | None = None
_modules_cache: dict[str, set[str]] = {}  # tenant_id -> enabled modules


def _control_engine() -> AsyncEngine:
    """Owner engine against the PLATFORM database (entitlements are a cell-local control-plane read)."""
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(s.owner_db_url(s.platform_db()), poolclass=NullPool, pool_pre_ping=True)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _modules_cache.clear()


async def ensure_schema() -> None:
    async with _control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS entitlement_plans (
                    plan_key   text PRIMARY KEY,
                    modules    jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tenant_entitlements (
                    tenant_id  uuid PRIMARY KEY,
                    plan_key   text NOT NULL,
                    overrides  jsonb NOT NULL DEFAULT '{}'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )


async def seed_plans() -> None:
    """Upsert the plan catalog (idempotent)."""
    await ensure_schema()
    async with _control_engine().begin() as conn:
        for plan_key, modules in PLAN_MODULES.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO entitlement_plans (plan_key, modules) VALUES (:k, :m)
                    ON CONFLICT (plan_key) DO UPDATE SET modules = EXCLUDED.modules, updated_at = now()
                    """
                ),
                {"k": plan_key, "m": json.dumps(sorted(modules))},
            )


async def set_tenant_plan(tenant_id: uuid.UUID | str, plan_key: str) -> None:
    if plan_key not in PLAN_MODULES:
        msg = f"unknown plan {plan_key!r}"
        raise ValueError(msg)
    tid = str(uuid.UUID(str(tenant_id)))
    await ensure_schema()
    async with _control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO tenant_entitlements (tenant_id, plan_key) VALUES (:t, :p)
                ON CONFLICT (tenant_id) DO UPDATE SET plan_key = EXCLUDED.plan_key, updated_at = now()
                """
            ),
            {"t": tid, "p": plan_key},
        )
    _modules_cache.pop(tid, None)


async def tenant_plan(tenant_id: uuid.UUID | str) -> str:
    tid = str(uuid.UUID(str(tenant_id)))
    async with _control_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT plan_key FROM tenant_entitlements WHERE tenant_id = :t"), {"t": tid}
            )
        ).first()
    return str(row.plan_key) if row else DEFAULT_PLAN


async def tenant_modules(tenant_id: uuid.UUID | str) -> set[str]:
    """The modules this tenant may use (cached). Unassigned tenants fall back to DEFAULT_PLAN."""
    tid = str(uuid.UUID(str(tenant_id)))
    cached = _modules_cache.get(tid)
    if cached is not None:
        return cached
    plan = await tenant_plan(tid)
    modules = set(PLAN_MODULES.get(plan, PLAN_MODULES[DEFAULT_PLAN]))
    _modules_cache[tid] = modules
    return modules


async def tenant_has_module(tenant_id: uuid.UUID | str, module: str) -> bool:
    return module in await tenant_modules(tenant_id)
