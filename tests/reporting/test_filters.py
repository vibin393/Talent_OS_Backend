"""Dashboard filters (A2): default range, range validation, BU scope, granularity, tile timeout, cache."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.settings import get_settings
from app.services.reporting import dashboards
from app.services.reporting.dashboards_p3 import P3_ORDER
from tests.reporting.helpers import API, IST, ROrg, tile


async def test_default_range_is_last_30_days(rorg: ROrg) -> None:
    out = await tile(rorg.manager, "manager", "open_demands")
    today = datetime.now(UTC).astimezone(IST).date()
    assert out["status"] == "ok"
    assert out["date_to"] == today.isoformat()
    assert out["date_from"] == (today - timedelta(days=29)).isoformat()
    assert out["computed_at"]


async def test_range_over_366_days_is_422(rorg: ROrg) -> None:
    resp = await rorg.manager.client.get(
        f"{API}/dashboards/manager/tiles/open_demands", params={"from": "2025-01-01", "to": "2026-09-30"}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_range"


async def test_from_after_to_is_422(rorg: ROrg) -> None:
    resp = await rorg.manager.client.get(
        f"{API}/dashboards/manager/tiles/open_demands", params={"from": "2026-03-10", "to": "2026-03-01"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_range"


async def test_exactly_366_days_is_allowed(rorg: ROrg) -> None:
    out = await tile(rorg.manager, "manager", "open_demands", **{"from": "2025-03-01", "to": "2026-03-01"})
    assert out["status"] == "ok"


async def test_manager_bu_outside_scope_is_403(rorg: ROrg) -> None:
    resp = await rorg.manager.client.get(
        f"{API}/dashboards/manager/tiles/open_demands", params={"business_unit_ids": str(rorg.bu_b)}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "bu_out_of_scope"
    ok = await tile(rorg.manager, "manager", "open_demands", business_unit_ids=str(rorg.bu_a))
    assert ok["link"]["query"]["business_unit_ids"] == [str(rorg.bu_a)]


async def test_tenant_admin_may_pick_any_bu(rorg: ROrg) -> None:
    out = await tile(
        rorg.admin, "manager", "open_demands", business_unit_ids=[str(rorg.bu_a), str(rorg.bu_b)]
    )
    assert out["status"] == "ok"


async def test_recruiter_ignores_bu_filter(rorg: ROrg) -> None:
    out = await tile(rorg.recruiter, "recruiter", "my_open_demands", business_unit_ids=str(rorg.bu_b))
    assert out["status"] == "ok"


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        ({"from": "2026-03-01", "to": "2026-03-31"}, "day"),
        ({"from": "2026-01-01", "to": "2026-03-31"}, "week"),
        ({"from": "2025-10-01", "to": "2026-09-30"}, "month"),
    ],
)
async def test_auto_granularity(rorg: ROrg, dates: dict[str, str], expected: str) -> None:
    out = await tile(rorg.manager, "manager", "throughput", **dates)
    assert out["granularity"] == expected


async def test_unknown_dashboard_or_tile_is_404(rorg: ROrg) -> None:
    assert (await rorg.manager.client.get(f"{API}/dashboards/nope/tiles/open_demands")).status_code == 404
    assert (await rorg.manager.client.get(f"{API}/dashboards/manager/tiles/nope")).status_code == 404
    # A recruiter cannot open the manager dashboard.
    assert (
        await rorg.recruiter.client.get(f"{API}/dashboards/manager/tiles/open_demands")
    ).status_code == 404


async def test_tile_timeout_returns_status_timeout(rorg: ROrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    async def slow(t: Any) -> dict[str, Any]:
        await t.db.execute(text("SELECT pg_sleep(0.2)"))
        return {"value": 1}

    manager = dashboards.DASHBOARDS["manager"]
    original = manager.tile("open_demands")
    assert original is not None
    import dataclasses

    patched = dataclasses.replace(original, fn=slow)
    monkeypatch.setattr(
        dashboards,
        "DASHBOARDS",
        {
            **dashboards.DASHBOARDS,
            "manager": dataclasses.replace(
                manager, tiles=tuple(patched if t.key == "open_demands" else t for t in manager.tiles)
            ),
        },
    )
    monkeypatch.setattr(get_settings(), "dashboard_tile_timeout_ms", 1)
    out = await tile(rorg.manager, "manager", "open_demands")
    assert out["status"] == "timeout"
    assert out.get("value") is None
    # The rest of the dashboard still renders.
    monkeypatch.setattr(get_settings(), "dashboard_tile_timeout_ms", 2500)
    assert (await tile(rorg.manager, "manager", "sla_risk"))["status"] == "ok"


async def test_cache_serves_until_refresh(rorg: ROrg, monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}

    async def get(key: str) -> Any:
        return store.get(key)

    async def put(key: str, value: Any, ttl: int) -> None:
        assert ttl == 60
        store[key] = value

    monkeypatch.setattr(get_settings(), "dashboard_cache_seconds", 60)
    monkeypatch.setattr(dashboards.cache, "get", get)
    monkeypatch.setattr(dashboards.cache, "set", put)
    url = f"{API}/dashboards/manager/tiles/open_demands"
    first = (await rorg.manager.client.get(url)).json()
    assert len(store) == 1
    key = next(iter(store))
    assert key.startswith(f"dash:{rorg.tenant_id}:{rorg.manager.user_id}:")
    store[key] = {**store[key], "value": 999}
    assert (await rorg.manager.client.get(url)).json()["value"] == 999
    fresh = (await rorg.manager.client.get(url, params={"refresh": "true"})).json()
    assert fresh["value"] == first["value"]


async def test_dashboards_listing_by_role(rorg: ROrg) -> None:
    async def keys(m: Any) -> list[str]:
        resp = await m.client.get(f"{API}/dashboards")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert items[0]["default"] is True
        all_keys = [d["key"] for d in items]
        # Epic 52: allowed Phase 3 dashboards follow the P1 / P2 ones (checked below for the Tenant Admin).
        p3 = [k for k in all_keys if k in P3_ORDER]
        assert all_keys[len(all_keys) - len(p3) :] == p3
        return [k for k in all_keys if k not in P3_ORDER]

    # Phase close (900_merge_phase2 resync): the P1 roles hold Phase 2 read keys, so their allowed P2
    # dashboards follow their own; the Account Manager's P2 default (account_ops) comes first (FR-81).
    assert await keys(rorg.recruiter) == ["recruiter", "account_ops", "onboarding", "compliance"]
    assert await keys(rorg.manager) == ["manager", "account_ops"]
    assert await keys(rorg.account) == ["account_ops", "account", "onboarding", "compliance"]
    # Epic 34: the Tenant Admin (every permission) also gets the Phase 2 dashboards, after the P1 ones.
    assert await keys(rorg.admin) == [
        "manager", "recruiter", "account", "finance", "account_ops", "onboarding", "compliance", "delivery", "executive",
    ]  # fmt: skip
    admin_all = [d["key"] for d in (await rorg.admin.client.get(f"{API}/dashboards")).json()["items"]]
    assert admin_all[-len(P3_ORDER) :] == list(P3_ORDER)
    listing = (await rorg.admin.client.get(f"{API}/dashboards")).json()["items"][0]
    assert {"key", "title", "kind", "size"} <= set(listing["tiles"][0])
