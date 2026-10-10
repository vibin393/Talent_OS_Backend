"""Epic 48 story 48.2 (A4): thresholds, baseline gate, promotion, reproducibility, settings floor."""

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.analytics import MlFeatureSpec, MlModel, MlTrainingSet
from app.services.analytics import training
from app.services.analytics.features import denylist
from app.services.analytics.models.serialize import dumps
from tests.modules.analytics.helpers import API, attrition_rows, offer_rows, train_with, ttf_rows
from tests.modules.demands.helpers import DOrg, h


async def test_150_outcomes_is_insufficient_with_progress(dorg: DOrg) -> None:
    model = await train_with(dorg.tenant_id, "offer_drop", offer_rows(150, 40))
    assert model.status == "insufficient_data"
    assert model.metrics["progress"] == {"n": 150, "required": 200, "n_minority": 40, "required_minority": 30}
    assert model.training_set_id is None  # no snapshot beyond the counts
    cards = await dorg.admin.client.get(f"{API}/analytics/models", params={"kind": "offer_drop"})
    card = cards.json()["cards"][0]
    assert (
        card["status"] == "insufficient_data"
        and card["progress"]["n"] == 150
        and card["progress"]["required"] == 200
    )


async def test_minority_threshold(dorg: DOrg) -> None:
    model = await train_with(dorg.tenant_id, "offer_drop", offer_rows(250, 20))
    assert model.status == "insufficient_data"
    assert (
        model.metrics["progress"]["n_minority"] == 20 and model.metrics["progress"]["required_minority"] == 30
    )


async def test_planted_signal_becomes_active_and_supersedes(dorg: DOrg) -> None:
    first = await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=1))
    assert first.status == "active", first.metrics
    assert first.n_samples == 400 and first.n_minority == 60
    hold = first.metrics["holdout"]
    assert float(hold["auc"]) >= 0.55 and float(hold["brier"]) < float(first.metrics["baseline"]["brier"])
    assert all(isinstance(v, str) for v in first.params["coefficients"].values())  # decimal strings only
    second = await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=2))
    assert second.status == "active" and second.version == first.version + 1
    async with tenant_session(dorg.tenant_id) as db:
        old = await db.get(MlModel, first.id)
        assert old is not None and old.status == "superseded" and old.superseded_at is not None
        spec = (
            await db.execute(select(MlFeatureSpec).where(MlFeatureSpec.kind == "offer_drop"))
        ).scalar_one()
        assert denylist.violations(spec.features) == []  # CI denylist over the stored spec rows
        ts = await db.get(MlTrainingSet, second.training_set_id)
        assert ts is not None and ts.row_count == 400 and ts.n_minority == 60
        assert ts.storage_key.startswith(f"t/{dorg.tenant_id}/")  # tenant storage prefix


async def test_noise_fails_below_baseline_and_keeps_previous(dorg: DOrg) -> None:
    good = await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=3))
    assert good.status == "active"
    noise = await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, signal=False, seed=4))
    assert noise.status == "failed" and noise.failure_code == "below_baseline"
    async with tenant_session(dorg.tenant_id) as db:
        kept = await db.get(MlModel, good.id)
        assert kept is not None and kept.status == "active"


async def test_time_to_fill_and_attrition_train(dorg: DOrg) -> None:
    ttf = await train_with(dorg.tenant_id, "time_to_fill", ttf_rows())
    assert ttf.status == "active", ttf.metrics
    assert float(ttf.metrics["holdout"]["mae_days"]) < float(ttf.metrics["baseline"]["mae_days"])
    assert float(ttf.params["residual_q20"]) < float(ttf.params["residual_q80"])
    att = await train_with(dorg.tenant_id, "attrition", attrition_rows())
    assert att.status == "active", att.metrics


async def test_reproducible_from_snapshot_and_seed(dorg: DOrg) -> None:
    model = await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=9))
    assert model.status == "active"
    async with tenant_session(dorg.tenant_id) as db:
        stored = await db.get(MlModel, model.id)
        assert stored is not None
        again = await training.refit(db, stored)
        assert dumps(again) == dumps(stored.params)


async def test_disabled_kind_does_not_train(dorg: DOrg) -> None:
    r = await dorg.admin.client.put(
        f"{API}/settings/analytics", json={"enabled_kinds": {"attrition": False}}, headers=h(dorg.admin)
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled_kinds"]["attrition"] is False
    async with tenant_session(dorg.tenant_id) as db:
        assert await training.train(db, dorg.tenant_id, "attrition", rows=attrition_rows()) is None


async def test_settings_cannot_go_below_platform_defaults(dorg: DOrg) -> None:
    a = dorg.admin
    low = await a.client.put(f"{API}/settings/analytics", json={"min_samples": 100}, headers=h(a))
    assert low.status_code == 422 and low.json()["error"]["code"] == "threshold_below_platform"
    low = await a.client.put(f"{API}/settings/analytics", json={"min_minority": 10}, headers=h(a))
    assert low.status_code == 422
    ok = await a.client.put(
        f"{API}/settings/analytics",
        json={
            "min_samples": 300,
            "band_thresholds": {
                "offer_drop": {"medium": "0.3", "high": "0.6"},
                "attrition": {"medium": "0.2", "high": "0.4"},
            },
        },
        headers=h(a),
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["min_samples"] == 300 and body["platform_min_samples"] == 200
    assert body["band_thresholds"]["offer_drop"] == {"medium": "0.3", "high": "0.6"}
    bad = await a.client.put(
        f"{API}/settings/analytics",
        json={
            "band_thresholds": {
                "offer_drop": {"medium": "0.7", "high": "0.6"},
                "attrition": {"medium": "0.2", "high": "0.4"},
            }
        },
        headers=h(a),
    )
    assert bad.status_code == 422
    model = await train_with(dorg.tenant_id, "offer_drop", offer_rows(250, 40))
    assert model.status == "insufficient_data" and model.metrics["progress"]["required"] == 300
