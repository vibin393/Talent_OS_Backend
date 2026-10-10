# ruff: noqa: E501
"""Epic 48 story 48.6 (A6, NFR-44): Claude only explains the labelled contributions; bad citations are discarded."""

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.analytics import MlPrediction
from app.services import ai
from app.services.analytics import scoring
from tests.epic3 import FakeAI
from tests.modules.analytics.helpers import API, train_with, ttf_rows
from tests.modules.demands.helpers import DOrg, h, open_demand

TITLE = "Principal Quantum Engineer"


async def _prediction(org: DOrg) -> tuple[str, str]:
    demand = await open_demand(org.admin, org.client_id, title=TITLE, mandatory_skills=["Qiskit"])
    async with tenant_session(org.tenant_id) as db:
        await scoring.score(db, "time_to_fill", [uuid.UUID(demand["id"])])
        p = (
            await db.execute(select(MlPrediction).where(MlPrediction.subject_id == uuid.UUID(demand["id"])))
        ).scalar_one()
        return str(p.id), demand["id"]


async def _enable(org: DOrg, on: bool = True) -> None:
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"prediction_explanation": on})


async def test_explanation_flow(dorg: DOrg, fake_ai: FakeAI) -> None:
    await train_with(dorg.tenant_id, "time_to_fill", ttf_rows())
    pred_id, demand_id = await _prediction(dorg)
    a = dorg.admin
    off = await a.client.post(f"{API}/predictions/{pred_id}/explanation", headers=h(a))
    assert (
        off.status_code == 200 and off.json()["status"] == "unavailable" and fake_ai.calls == []
    )  # AI off -> table only
    await _enable(dorg)
    fake_ai.payload = {
        "text": "More required skills raise the expected time to fill. This is a supporting signal, not a decision.",
        "cited_feature_keys": ["required_skills"],
    }
    ok = await a.client.post(f"{API}/predictions/{pred_id}/explanation", headers=h(a))
    assert (
        ok.json()["status"] == "ok" and "supporting signal" in ok.json()["text"] and ok.json()["disclaimer"]
    )
    assert len(fake_ai.calls) == 1
    sent: dict[str, Any] = fake_ai.calls[0]
    rendered = json.dumps(sent["messages"]) + sent["system"]
    assert TITLE not in rendered and demand_id not in rendered and "@" not in rendered
    assert "required_skills | Number of required skills" in rendered
    again = await a.client.post(f"{API}/predictions/{pred_id}/explanation", headers=h(a))
    assert again.json()["text"] == ok.json()["text"] and len(fake_ai.calls) == 1  # cached
    r = await a.client.get(f"{API}/predictions", params={"subject_kind": "demand", "subject_id": demand_id})
    assert r.json()["predictions"][0]["explanation_status"] == "ok"


async def test_citation_outside_the_spec_is_discarded(dorg: DOrg, fake_ai: FakeAI) -> None:
    await train_with(dorg.tenant_id, "time_to_fill", ttf_rows())
    pred_id, _ = await _prediction(dorg)
    await _enable(dorg)
    fake_ai.payload = {"text": "The candidate pool is small.", "cited_feature_keys": ["candidate_gender"]}
    r = await dorg.admin.client.post(f"{API}/predictions/{pred_id}/explanation", headers=h(dorg.admin))
    assert r.json() == {"status": "unavailable", "text": None, "disclaimer": r.json()["disclaimer"]}
    fake_ai.payload = {"text": "Risk is higher because of age.", "cited_feature_keys": ["required_skills"]}
    r = await dorg.admin.client.post(f"{API}/predictions/{pred_id}/explanation", headers=h(dorg.admin))
    assert r.json()["status"] == "unavailable"  # denylisted token in the text
    async with tenant_session(dorg.tenant_id) as db:
        p = await db.get(MlPrediction, uuid.UUID(pred_id))
        assert p is not None and p.explanation_text is None and p.explanation_status == "unavailable"
