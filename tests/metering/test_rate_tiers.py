# ruff: noqa: E501
"""Rate Tier constants, overrides and the resolution order (spec 0.12)."""

import uuid

from sqlalchemy import text

from app.core.database import platform_session, tenant_session
from app.core.rate_tiers import TIERS, resolve_tier_name, tier
from app.services.metering import enforce
from tests.metering.conftest import set_tenant


def test_spec_values() -> None:
    assert (TIERS["sandbox"].per_minute, TIERS["sandbox"].burst_per_second, TIERS["sandbox"].per_day) == (
        60,
        10,
        5_000,
    )
    assert TIERS["standard"].per_day == 200_000 and TIERS["standard"].delegated_user_per_min == 120
    assert (
        TIERS["enterprise"].per_minute == 3_000 and TIERS["enterprise"].mcp_calls_per_min_per_session == 120
    )


def test_overrides_and_unknown_names() -> None:
    assert tier("standard", {"standard": {"per_minute": 900}}).per_minute == 900
    assert tier("standard", {"standard": {"per_minute": 900}}).per_day == 200_000
    assert tier("nope", {}).name == "standard"
    custom = tier("gold", {"gold": {"per_day": 7}})
    assert custom.name == "gold" and custom.per_day == 7 and custom.per_minute == 600


def test_resolution_order() -> None:
    kw = {
        "installation_tier": "enterprise",
        "app_tier": "sandbox",
        "ecosystem_tier": "x",
        "tenant_tier": "standard",
    }
    assert resolve_tier_name(**kw, sandbox=False) == "enterprise"
    assert resolve_tier_name(**{**kw, "installation_tier": None}, sandbox=False) == "sandbox"
    assert resolve_tier_name(**{**kw, "installation_tier": None, "app_tier": " "}, sandbox=False) == "x"
    assert (
        resolve_tier_name(
            installation_tier=None,
            app_tier=None,
            ecosystem_tier=None,
            tenant_tier="enterprise",
            sandbox=False,
        )
        == "enterprise"
    )
    assert resolve_tier_name(**kw, sandbox=True) == "sandbox"


async def _app_and_installation(
    tenant_id: uuid.UUID, *, app_tier: str | None, inst_tier: str | None
) -> uuid.UUID:
    app_id, inst_id = uuid.uuid4(), uuid.uuid4()
    async with platform_session() as db:
        await db.execute(
            text(
                "INSERT INTO apps (id, client_id, name, slug, publisher_kind, client_type, grant_types, requested_scopes, "
                "manifest, manifest_sha256, created_by_kind, rate_tier, created_at, updated_at) VALUES (:id, :cid, 'T', "
                ":slug, 'platform', 'confidential', ARRAY['client_credentials']::varchar[], ARRAY[]::varchar[], "
                "'{}'::jsonb, :sha, 'platform', :tier, now(), now())"
            ),
            {
                "id": app_id,
                "cid": f"tosapp_{app_id.hex[:26]}",
                "slug": f"m-{app_id.hex[:12]}",
                "sha": "0" * 64,
                "tier": app_tier,
            },
        )
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO app_installations (id, tenant_id, kind, app_id, display_name, manifest_version, "
                "manifest_sha256, status, rate_tier, created_at, updated_at) VALUES (:id, :t, 'app', :app, 'T', 1, :sha, "
                "'active', :tier, now(), now())"
            ),
            {"id": inst_id, "t": tenant_id, "app": app_id, "sha": "0" * 64, "tier": inst_tier},
        )
    return inst_id


async def test_resolve_tier_reads_the_chain(tenant: uuid.UUID) -> None:
    enforce.clear_tier_cache()
    assert (await enforce.resolve_tier(tenant)).name == "standard"
    await set_tenant(tenant, rate_tier="enterprise")
    enforce.clear_tier_cache()
    assert (await enforce.resolve_tier(tenant)).name == "enterprise"
    async with tenant_session(tenant) as db:
        await db.execute(
            text(
                "INSERT INTO ecosystem_settings (id, tenant_id, rate_tier, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :t, 'sandbox', now(), now())"
            ),
            {"t": tenant},
        )
    enforce.clear_tier_cache()
    assert (await enforce.resolve_tier(tenant)).name == "sandbox"
    inst_app = await _app_and_installation(tenant, app_tier="standard", inst_tier=None)
    assert (await enforce.resolve_tier(tenant, inst_app)).name == "standard"
    inst = await _app_and_installation(tenant, app_tier="standard", inst_tier="enterprise")
    assert (await enforce.resolve_tier(tenant, inst)).name == "enterprise"
    # cached for 60 s; a sandbox tenant is always sandbox once the cache is cleared
    await set_tenant(tenant, mode="sandbox", sandbox_owner_kind="platform")
    assert (await enforce.resolve_tier(tenant, inst)).name == "enterprise"
    enforce.clear_tier_cache(tenant)
    assert (await enforce.resolve_tier(tenant, inst)).name == "sandbox"
