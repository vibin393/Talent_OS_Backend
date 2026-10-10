# ruff: noqa: E501, RUF001
"""Epic 48 stories 48.3-48.5 (A5): scoring, intervals, bands, band-change events, debounce, reads and the
attrition permission gate."""

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import pending_events
from app.core.permissions import CATALOG, Principal, Scope
from app.events.analytics import PredictionBandChanged
from app.models.analytics import MlPrediction
from app.services.analytics import governance, hooks, reads, scoring
from app.services.analytics import models as ml
from app.services.analytics.features.specs import OFFER_DROP, TIME_TO_FILL
from tests.modules.analytics.helpers import API, attrition_rows, offer_rows, train_with, ttf_rows
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.placements.helpers import direct_placement


# ---------------------------------------------------------------- pure arithmetic
def test_contributions_sum_to_the_linear_term() -> None:
    rows = offer_rows(400, 60)
    fit = ml.fit(
        "classification",
        OFFER_DROP.as_json(),
        [(r.features, r.label) for r in rows[:320]],
        [(r.features, r.label) for r in rows[320:]],
        48,
    )
    for r in rows[:25]:
        p = ml.predict(fit.params, r.features)
        assert abs(p.intercept + sum(p.contributions.values()) - p.linear) < 1e-9
        assert abs(p.probability - 1 / (1 + math.exp(-p.linear))) < 1e-12  # type: ignore[operator]
    t = ttf_rows()
    reg = ml.fit(
        "regression",
        TIME_TO_FILL.as_json(),
        [(r.features, r.label) for r in t[:240]],
        [(r.features, r.label) for r in t[240:]],
        48,
    )
    for r in t[:25]:
        p = ml.predict(reg.params, r.features)
        assert abs(p.intercept + sum(p.contributions.values()) - p.linear) < 1e-9
        assert p.p20 <= p.days <= p.p80  # type: ignore[operator]


def test_pct_band_never_shows_an_amount() -> None:
    assert scoring.pct_band(23.4) == "+20–30 %"
    assert scoring.pct_band(-12.0) == "-10–20 %"


# ---------------------------------------------------------------- helpers
def ctx_for(org: DOrg, *, drop: tuple[str, ...] = ()) -> RequestContext:
    perms = {k: Scope.TENANT for k in CATALOG if k not in drop}
    principal = Principal(type="user", id=org.admin.user_id, tenant_id=org.tenant_id, permissions=perms)
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def _score(org: DOrg, kind: str, ids: list[uuid.UUID] | None = None) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        await scoring.score(db, kind, ids)
        return [e for e in pending_events(db) if isinstance(e, PredictionBandChanged)]


async def _activate(org: DOrg, placement_id: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE placements SET status = 'active', actual_start_date = :d, planned_end_date = :e WHERE id = :i"
            ),
            {
                "d": (datetime.now(UTC) - timedelta(days=90)).date(),
                "e": (datetime.now(UTC) + timedelta(days=120)).date(),
                "i": uuid.UUID(placement_id),
            },
        )


# ---------------------------------------------------------------- time to fill (48.3)
async def test_open_demand_gets_days_and_range(dorg: DOrg) -> None:
    model = await train_with(dorg.tenant_id, "time_to_fill", ttf_rows())
    assert model.status == "active"
    demand = await open_demand(dorg.admin, dorg.client_id, mandatory_skills=["PySpark", "Kafka", "Scala"])
    await _score(dorg, "time_to_fill")
    r = await dorg.admin.client.get(
        f"{API}/predictions", params={"subject_kind": "demand", "subject_id": demand["id"]}
    )
    assert r.status_code == 200, r.text
    [entry] = r.json()["predictions"]
    assert (
        entry["kind"] == "time_to_fill" and entry["status"] == "ok" and entry["label"] == "Supporting signal"
    )
    assert entry["model_id"] == str(model.id) and entry["model_version"] == model.version
    assert entry["band"] is None
    assert (
        Decimal(entry["interval"]["p20"])
        <= Decimal(entry["predicted_days"])
        <= Decimal(entry["interval"]["p80"])
    )
    keys = [c["key"] for c in entry["contributions"]]
    assert set(keys) == set(TIME_TO_FILL.keys)
    assert all(c["display"].endswith("days") for c in entry["contributions"])
    batch = await dorg.admin.client.get(
        f"{API}/predictions/batch",
        params={"subject_kind": "demand", "ids": [demand["id"], str(uuid.uuid4())]},
    )
    assert batch.status_code == 200 and [i["subject_id"] for i in batch.json()["items"]] == [demand["id"]]


async def test_insufficient_and_disabled_return_status_only(dorg: DOrg) -> None:
    demand = await open_demand(dorg.admin, dorg.client_id)
    r = await dorg.admin.client.get(
        f"{API}/predictions", params={"subject_kind": "demand", "subject_id": demand["id"]}
    )
    assert r.json()["predictions"][0]["status"] == "no_model"
    await train_with(dorg.tenant_id, "time_to_fill", ttf_rows(150))
    r = await dorg.admin.client.get(
        f"{API}/predictions", params={"subject_kind": "demand", "subject_id": demand["id"]}
    )
    entry = r.json()["predictions"][0]
    assert entry["status"] == "insufficient_data" and entry["progress"] == {
        "n": 150,
        "required": 200,
        "n_minority": None,
        "required_minority": None,
    }
    assert entry["score"] is None and entry["contributions"] == []
    await dorg.admin.client.put(
        f"{API}/settings/analytics", json={"enabled_kinds": {"time_to_fill": False}}, headers=h(dorg.admin)
    )
    r = await dorg.admin.client.get(
        f"{API}/predictions", params={"subject_kind": "demand", "subject_id": demand["id"]}
    )
    assert r.json()["predictions"] == [{**r.json()["predictions"][0], "status": "disabled"}]
    assert r.json()["predictions"][0]["score"] is None


# ---------------------------------------------------------------- offer drop (48.4)
async def test_band_change_emits_once(dorg: DOrg) -> None:
    placement = await direct_placement(
        dorg
    )  # created before the model exists: the event-driven score is a no-op
    pid = uuid.UUID(placement["id"])
    a = dorg.admin
    await a.client.put(
        f"{API}/settings/analytics",
        json={
            "band_thresholds": {
                "offer_drop": {"medium": "0.0001", "high": "0.0002"},
                "attrition": {"medium": "0.25", "high": "0.5"},
            }
        },
        headers=h(a),
    )
    await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=21))
    first = await _score(dorg, "offer_drop", [pid])
    assert [(e.from_band, e.to_band, e.subject_kind) for e in first] == [(None, "high", "placement")]
    assert await _score(dorg, "offer_drop", [pid]) == []  # same band: no event
    await a.client.put(
        f"{API}/settings/analytics",
        json={
            "band_thresholds": {
                "offer_drop": {"medium": "0.998", "high": "0.999"},
                "attrition": {"medium": "0.25", "high": "0.5"},
            }
        },
        headers=h(a),
    )
    third = await _score(dorg, "offer_drop", [pid])
    assert [(e.from_band, e.to_band) for e in third] == [("high", "low")]
    r = await a.client.get(f"{API}/predictions", params={"subject_kind": "placement", "subject_id": str(pid)})
    entry = next(e for e in r.json()["predictions"] if e["kind"] == "offer_drop")
    assert entry["band"] == "low" and 0 <= float(entry["score"]) <= 1


def test_debounce_collapses_events(monkeypatch: pytest.MonkeyPatch) -> None:
    held: set[str] = set()

    def acquire(tenant_id: uuid.UUID, kind: str, sid: uuid.UUID) -> bool:
        key = f"{tenant_id}:{kind}:{sid}"
        if key in held:
            return False
        held.add(key)
        return True

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(hooks, "debounce_acquire", acquire)
    import app.tasks.runtime as runtime

    monkeypatch.setattr(runtime, "enqueue", lambda task, *args, **kw: calls.append(args))
    tid, sid = uuid.uuid4(), uuid.uuid4()
    for _ in range(10):
        hooks.mark_dirty(tid, "demand", sid)
    assert calls == [(str(tid), "demand", str(sid))]


# ---------------------------------------------------------------- attrition gate (48.5)
async def _attrition_prediction(org: DOrg) -> uuid.UUID:
    await train_with(org.tenant_id, "attrition", attrition_rows())
    placement = await direct_placement(org)
    await _activate(org, placement["id"])
    pid = uuid.UUID(placement["id"])
    await _score(org, "attrition", [pid])
    return pid


async def test_attrition_absent_without_permission(dorg: DOrg) -> None:
    pid = await _attrition_prediction(dorg)
    r = await dorg.admin.client.get(
        f"{API}/predictions", params={"subject_kind": "placement", "subject_id": str(pid)}
    )
    assert [e["kind"] for e in r.json()["predictions"]] == ["attrition"]
    async with tenant_session(dorg.tenant_id) as db:
        out = await reads.predictions_for(
            ctx_for(dorg, drop=("prediction:read_attrition",)), db, "placement", pid
        )
        dumped = out.model_dump_json()
        assert out.predictions == [] and "attrition" not in dumped
        batch = await reads.batch(ctx_for(dorg, drop=("prediction:read_attrition",)), db, "placement", [pid])
        assert batch.items == []
        with pytest.raises(Exception) as exc:
            await reads.predictions_for(
                ctx_for(dorg, drop=("prediction:read_attrition", "prediction:read")), db, "placement", pid
            )
        assert getattr(exc.value, "status_code", None) == 403
        p = (await db.execute(select(MlPrediction).where(MlPrediction.subject_id == pid))).scalar_one()
        with pytest.raises(Exception) as exc:
            await reads.readable_prediction(ctx_for(dorg, drop=("prediction:read_attrition",)), db, p.id)
        assert getattr(exc.value, "status_code", None) == 404


async def test_model_cards_full_and_reduced(dorg: DOrg) -> None:
    await train_with(dorg.tenant_id, "offer_drop", offer_rows(400, 60, seed=1))
    full = await dorg.admin.client.get(f"{API}/analytics/models")
    cards = {c["kind"]: c for c in full.json()["cards"]}
    od = cards["offer_drop"]
    assert od["status"] == "active" and not od["reduced"] and od["active"]["metrics"]["auc"]
    assert od["active"]["baseline"]["auc"] == "0.5" and od["active"]["calibration"]
    assert od["active"]["features"][0]["rank"] == 1 and od["excluded_features"]
    assert cards["time_to_fill"]["status"] == "no_model"
    async with tenant_session(dorg.tenant_id) as db:
        reduced = await governance.model_cards(
            ctx_for(dorg, drop=("model:manage", "prediction:read_attrition")), db
        )
    kinds = {c.kind: c for c in reduced}
    assert (
        "attrition" not in kinds and kinds["offer_drop"].reduced and kinds["offer_drop"].active.metrics == {}
    )  # type: ignore[union-attr]


def test_portal_principals_never_get_prediction_fields() -> None:
    from app.core.field_visibility import FORBIDDEN_FIELDS, is_forbidden

    for kind in FORBIDDEN_FIELDS:
        for field in ("prediction", "prediction_band", "attrition_risk", "drop_risk", "time_to_fill_days"):
            assert is_forbidden(kind, field), (kind, field)


def test_no_analytics_schema_is_a_portal_schema() -> None:
    import app.schemas.analytics as schemas
    from app.core.field_visibility import registered_schemas

    portal = set(registered_schemas())
    assert not {getattr(schemas, n) for n in dir(schemas) if isinstance(getattr(schemas, n), type)} & portal
    assert json.dumps(
        sorted(m.__name__ for m in portal)
    )  # registry is populated by the portal tests' imports
