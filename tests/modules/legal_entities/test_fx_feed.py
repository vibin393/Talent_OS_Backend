# ruff: noqa: E501
"""Epic 51 (FR-255): live FX feed -- fake adapter inserts feed rows, manual rows untouched, precedence both ways,
2-day vs 4-day outage, alert once per day, Manual adapter no-op, feed disabled = P2 behaviour, settings API."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.core.events import subscribe, unsubscribe
from app.events.legal_entities import FxFeedFailed
from app.models.connectors import ProviderConnectorConfig
from app.models.legal_entities import FxFeedSettings, FxRate
from app.schemas.fx_feed import FxFeedSettingsIn
from app.schemas.legal_entities import FxRateIn
from app.services import legal_entities_service
from app.services.connectors.fx.fake import FakeFxRateSource, fake_rate
from app.services.legal_entities import fx_feed
from tests.modules.identity.conftest import csrf
from tests.modules.legal_entities.conftest import API, LeOrg

D0 = date(2026, 9, 21)  # a Monday


@pytest.fixture(autouse=True)
def _reset() -> Any:
    FakeFxRateSource.reset()
    yield
    FakeFxRateSource.reset()


def _ctx(org: LeOrg) -> RequestContext:
    return RequestContext(tenant_id=org.tenant_id)


async def _fake(org: LeOrg) -> None:
    async with tenant_session(org.tenant_id) as db:
        db.add(
            ProviderConnectorConfig(
                tenant_id=org.tenant_id,
                category="fx",
                provider_key="fake",
                display_name="Fake FX",
                settings={},
                status="active",
            )
        )


async def _configure(org: LeOrg, **extra: Any) -> None:
    async with tenant_session(org.tenant_id) as db:
        await fx_feed.update_settings(
            _ctx(org), db, FxFeedSettingsIn(enabled=True, pairs=["USD/INR", "EUR/INR"], **extra)
        )


async def _fetch(org: LeOrg, on: date) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return await fx_feed.fetch(_ctx(org), db, on)


async def _rate(org: LeOrg, on: date, a: str = "USD", b: str = "INR") -> Any:
    async with tenant_session(org.tenant_id) as db:
        return await legal_entities_service.rate_for(_ctx(org), db, a, b, on)


async def test_feed_rows_inserted_manual_untouched_and_precedence(leorg: LeOrg) -> None:
    async with tenant_session(leorg.tenant_id) as db:
        await legal_entities_service.upsert_rate(
            _ctx(leorg),
            db,
            FxRateIn(
                from_currency="USD",
                to_currency="INR",
                rate_date=D0,
                rate=Decimal("83.5"),
                source="RBI reference",
            ),
        )
    async with tenant_session(leorg.tenant_id) as db:
        manual_before = (await db.execute(select(FxRate).where(FxRate.origin == "manual"))).scalar_one()
        manual_snapshot = (manual_before.rate, manual_before.source, manual_before.updated_at)
    await _fake(leorg)
    await _configure(leorg)
    run = await _fetch(leorg, D0)
    assert (
        run.status == "ok"
        and run.fetched == 2
        and run.inserted == 2
        and run.skipped_manual == 1
        and run.provider_key == "fake"
    )
    async with tenant_session(leorg.tenant_id) as db:
        rows = list(
            (await db.execute(select(FxRate).order_by(FxRate.from_currency, FxRate.origin))).scalars()
        )
        assert [(r.from_currency, r.origin, r.source) for r in rows] == [
            ("EUR", "feed", "feed:fake"),
            ("USD", "feed", "feed:fake"),
            ("USD", "manual", "RBI reference"),
        ]
        manual = next(r for r in rows if r.origin == "manual")
        assert (manual.rate, manual.source, manual.updated_at) == manual_snapshot
    # re-fetch the same date: still one feed row per pair, manual untouched
    await _fetch(leorg, D0)
    async with tenant_session(leorg.tenant_id) as db:
        assert len(list((await db.execute(select(FxRate))).scalars())) == 3
    # precedence: manual first (default, coordinator decision 2) vs feed first
    picked = await _rate(leorg, D0)
    assert picked.rate == Decimal("83.500000") and picked.source == "RBI reference"
    await _configure(leorg, precedence="feed_first")
    picked = await _rate(leorg, D0)
    assert picked.rate == fake_rate("USD", "INR", D0) and picked.source == "feed:fake"
    # the manual endpoint only ever writes manual rows
    async with tenant_session(leorg.tenant_id) as db:
        out = await legal_entities_service.upsert_rate(
            _ctx(leorg),
            db,
            FxRateIn(from_currency="USD", to_currency="INR", rate_date=D0, rate=Decimal("84"), source="Bank"),
        )
        assert out.origin == "manual"
        feed = (
            await db.execute(select(FxRate).where(FxRate.origin == "feed", FxRate.from_currency == "USD"))
        ).scalar_one()
        assert feed.rate == fake_rate("USD", "INR", D0)
    listed = await leorg.admin.client.get(f"{API}/fx-rates?from_currency=USD")
    assert sorted(r["origin"] for r in listed.json()) == ["feed", "manual"]


async def test_outages_staleness_and_alert_once_per_day(leorg: LeOrg) -> None:
    await _fake(leorg)
    await _configure(leorg)
    await _fetch(leorg, D0)
    alerts: list[FxFeedFailed] = []

    def on_failed(event: FxFeedFailed) -> None:
        alerts.append(event)

    subscribe(FxFeedFailed, on_failed)
    try:
        FakeFxRateSource.fail_ops.add("fetch_rates")
        for day in (date(2026, 9, 22), date(2026, 9, 22), date(2026, 9, 23)):
            run = await _fetch(leorg, day)
            assert run.status == "failed" and run.error
    finally:
        unsubscribe(FxFeedFailed, on_failed)
    assert [(a.on_date, a.consecutive_days) for a in alerts] == [
        (date(2026, 9, 22), 1),
        (date(2026, 9, 23), 2),
    ]
    async with tenant_session(leorg.tenant_id) as db:
        assert (await db.get(FxFeedSettings, leorg.tenant_id)).consecutive_failures == 2  # type: ignore[union-attr]
    # 2-day outage: the latest feed rate (<= 3 days old) is used
    two = await _rate(leorg, date(2026, 9, 23))
    assert two.rate_date == D0 and two.source == "feed:fake"
    # 4-day outage, no manual rate: FX documents need a manual rate
    with pytest.raises(ValidationFailed) as exc:
        await _rate(leorg, date(2026, 9, 25))
    assert exc.value.code == "fx_rate_missing"
    # recovery resets the counter
    FakeFxRateSource.fail_ops.clear()
    assert (await _fetch(leorg, date(2026, 9, 24))).status == "ok"
    async with tenant_session(leorg.tenant_id) as db:
        assert (await db.get(FxFeedSettings, leorg.tenant_id)).consecutive_failures == 0  # type: ignore[union-attr]


async def test_disabled_feed_is_p2_behaviour_and_manual_adapter_noop(leorg: LeOrg) -> None:
    # Manual adapter (no connector config): an ok run with no rows
    await _configure(leorg)
    run = await _fetch(leorg, D0)
    assert run.status == "ok" and run.fetched == 0 and run.provider_key == "manual"
    # feed rows exist but the feed is disabled -> rate_for sees manual rows only
    await _fake(leorg)
    await _fetch(leorg, D0)
    async with tenant_session(leorg.tenant_id) as db:
        await fx_feed.update_settings(_ctx(leorg), db, FxFeedSettingsIn(enabled=False, pairs=["USD/INR"]))
    with pytest.raises(ValidationFailed):
        await _rate(leorg, D0)
    async with tenant_session(leorg.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await fx_feed.fetch(_ctx(leorg), db, D0, manual_trigger=True)
        assert exc.value.code == "fx_feed_disabled"


def test_is_due_business_day_after_fetch_hour() -> None:
    row = FxFeedSettings(enabled=True, fetch_hour_ist=10)
    assert fx_feed.is_due(row, datetime(2026, 9, 21, 5, 0, tzinfo=UTC))  # Mon 10:30 IST
    assert not fx_feed.is_due(row, datetime(2026, 9, 21, 4, 0, tzinfo=UTC))  # Mon 09:30 IST
    assert not fx_feed.is_due(row, datetime(2026, 9, 26, 6, 0, tzinfo=UTC))  # Saturday
    row.enabled = False
    assert not fx_feed.is_due(row, datetime(2026, 9, 21, 6, 0, tzinfo=UTC))


async def test_settings_api_and_isolation(leorg: LeOrg, other_leorg: LeOrg) -> None:
    c = leorg.admin.client
    got = (await c.get(f"{API}/fx-feed/settings")).json()
    assert (
        got["enabled"] is False
        and got["precedence"] == "manual_first"
        and got["max_stale_days"] == 3
        and got["provider_key"] == "manual"
    )
    bad = await c.put(f"{API}/fx-feed/settings", json={"enabled": True, "pairs": ["USDINR"]}, headers=csrf(c))
    assert bad.status_code == 422
    bad = await c.put(
        f"{API}/fx-feed/settings",
        json={"enabled": True, "pairs": ["USD/INR"], "max_stale_days": 9},
        headers=csrf(c),
    )
    assert bad.status_code == 422
    put = await c.put(
        f"{API}/fx-feed/settings",
        json={
            "enabled": True,
            "pairs": ["usd/inr", "GBP/INR"],
            "precedence": "feed_first",
            "max_stale_days": 2,
            "fetch_hour_ist": 9,
        },
        headers=csrf(c),
    )
    assert put.status_code == 200, put.text
    assert put.json()["pairs"] == ["GBP/INR", "USD/INR"] and put.json()["precedence"] == "feed_first"
    now = await c.post(f"{API}/fx-feed/fetch-now", headers=csrf(c))
    assert now.status_code == 200 and now.json()["status"] == "ok"
    runs = (await c.get(f"{API}/fx-feed/runs")).json()
    assert len(runs) == 1
    other = other_leorg.admin.client
    assert (await other.get(f"{API}/fx-feed/settings")).json()["enabled"] is False
    assert (await other.get(f"{API}/fx-feed/runs")).json() == []
