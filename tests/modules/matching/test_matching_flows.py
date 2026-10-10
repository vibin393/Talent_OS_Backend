"""Stories 5.4/5.5: matching runs on Open demands, refresh, consent, dismiss/restore, pipeline, stale, settings,
explanations, rediscovery funnel and cross-tenant isolation. Celery runs eagerly, so every event-driven step
has completed when the triggering request returns."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.embeddings import CandidateEmbedding
from app.models.matching import Match
from app.services import ai
from app.services.candidates import privacy_port
from app.services.matching import explain, ports
from tests.epic3 import FakeAI
from tests.epic5 import DATA_ENGINEER, h, make_candidate, patch_candidate, search_ok, system_ctx
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.identity.conftest import join

DEMANDS = "/api/v1/demands"


@pytest.fixture(autouse=True)
def _unavailable_pipeline() -> Iterator[None]:
    previous = ports.pipeline_gateway()
    ports.register_pipeline_gateway(None)
    yield
    ports.register_pipeline_gateway(previous)


class FakePipeline:
    def __init__(self) -> None:
        self.added: list[tuple[uuid.UUID, uuid.UUID]] = []

    def is_available(self) -> bool:
        return True

    async def add_candidate(
        self,
        ctx: Any,
        db: Any,
        demand_id: uuid.UUID,
        candidate_id: uuid.UUID,
        *,
        source: str = "ai_match",
        match_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self.added.append((demand_id, candidate_id))
        return uuid.uuid5(uuid.NAMESPACE_OID, f"{demand_id}:{candidate_id}")

    async def candidate_ids_for_demand(self, ctx: Any, db: Any, demand_id: uuid.UUID) -> set[uuid.UUID]:
        return {c for d, c in self.added if d == demand_id}


async def _seed(org: DOrg) -> dict[str, dict[str, Any]]:
    m = org.admin
    return {
        "priya": await make_candidate(m, "Priya Raman", **DATA_ENGINEER),
        "jose": await make_candidate(
            m,
            "Jose Alvarez",
            current_title="Data Engineer",
            location={"city": "Pune", "country": "India"},
            total_experience_months=96,
            skills=[{"name": "Databricks", "years": 6}],
            notice_period_days=90,
        ),
        "anita": await make_candidate(m, "Anita Desai", current_title="Java Developer", skills=["Java"]),
    }


async def _open(org: DOrg, **overrides: Any) -> dict[str, Any]:
    body = {
        "title": "Senior Data Engineer",
        "mandatory_skills": ["Databricks", "PySpark", "ADF"],
        "min_experience_years": "4",
        "max_experience_years": "8",
        "location": "Chennai",
        "country": "IN",
        **overrides,
    }
    return await open_demand(org.admin, org.client_id, **body)


async def _matches(m: Any, demand_id: str, **params: Any) -> dict[str, Any]:
    resp = await m.client.get(f"{DEMANDS}/{demand_id}/matches", params=params)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["candidate"]["display_name"]: i for i in result["items"]}


async def test_open_demand_runs_matching_with_funnel_and_rediscovery(dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)
    result = await _matches(dorg.admin, demand["id"])
    rows = _by_name(result)
    assert "Priya Raman" in rows and "Jose Alvarez" in rows
    assert "Anita Desai" not in rows or rows["Anita Desai"]["overall"] < rows["Priya Raman"]["overall"]
    priya = rows["Priya Raman"]
    assert result["items"][0]["candidate"]["display_name"] == "Priya Raman"
    assert set(priya["sub_scores"]) == {
        "skills",
        "experience",
        "location",
        "compensation",
        "availability",
        "domain",
        "similarity",
    }
    assert priya["missing_mandatory"] == []
    assert rows["Jose Alvarez"]["missing_mandatory"] == ["PySpark", "Azure Data Factory"]
    assert rows["Jose Alvarez"]["sub_scores"]["skills"] <= 20
    assert priya["candidate"]["human_id"].startswith("CAN-")
    assert result["last_run"]["status"] == "completed"
    assert result["can_add_to_pipeline"] is False
    funnel = result["funnel"]
    assert funnel["total"] == 3 and funnel["relevant"] >= 2 and funnel["strong"] <= funnel["relevant"]
    assert funnel["available"] <= funnel["strong"]
    resp = await dorg.admin.client.get(f"{DEMANDS}/{demand['id']}/matches/funnel")
    assert resp.status_code == 200 and resp.json()["funnel"] == funnel
    timeline = (await dorg.admin.client.get(f"{DEMANDS}/{demand['id']}/timeline")).json()
    bodies = [e["body"] for e in timeline["items"]]
    assert any(b.startswith("Rediscovery: 3 → ") for b in bodies), bodies


async def test_candidate_edit_refreshes_matches(dorg: DOrg) -> None:
    seeded = await _seed(dorg)
    demand = await _open(dorg)
    before = _by_name(await _matches(dorg.admin, demand["id"]))["Jose Alvarez"]
    await patch_candidate(
        dorg.admin,
        seeded["jose"]["id"],
        skills=[
            {"name": "Databricks", "years": 6},
            {"name": "PySpark", "years": 5},
            {"name": "ADF", "years": 5},
        ],
    )
    after = _by_name(await _matches(dorg.admin, demand["id"]))["Jose Alvarez"]
    assert after["missing_mandatory"] == []
    assert after["overall"] > before["overall"]
    # a new candidate is scored against the open demand as soon as it is created
    await make_candidate(dorg.admin, "New Comer", **DATA_ENGINEER)
    assert "New Comer" in _by_name(await _matches(dorg.admin, demand["id"]))


async def test_consent_withdrawal_deletes_embeddings_and_matches(dorg: DOrg) -> None:
    seeded = await _seed(dorg)
    demand = await _open(dorg)
    cid = uuid.UUID(seeded["priya"]["id"])
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).first()
        await privacy_port.set_ai_consent_state(db, {cid: "withdrawn"})
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).first() is None
        assert (await db.execute(select(Match).where(Match.candidate_id == cid))).first() is None
    assert "Priya Raman" not in _by_name(await _matches(dorg.admin, demand["id"]))
    # still findable by keyword search
    assert "Priya Raman" in [
        i["display_name"] for i in (await search_ok(dorg.admin, q="databricks"))["items"]
    ]
    # a recompute does not bring it back
    resp = await dorg.admin.client.post(f"{DEMANDS}/{demand['id']}/matches/recompute", headers=h(dorg.admin))
    assert resp.status_code == 202, resp.text
    assert "Priya Raman" not in _by_name(await _matches(dorg.admin, demand["id"]))


async def test_dismiss_restore_audited(dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)
    match = _by_name(await _matches(dorg.admin, demand["id"]))["Jose Alvarez"]
    resp = await dorg.admin.client.post(
        f"/api/v1/matches/{match['id']}/dismiss",
        json={"reason": "skills_gap", "note": "No ADF"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dismiss_reason"] == "skills_gap" and resp.json()["dismissed_at"]
    assert "Jose Alvarez" not in _by_name(await _matches(dorg.admin, demand["id"]))
    assert "Jose Alvarez" in _by_name(await _matches(dorg.admin, demand["id"], include_dismissed="true"))
    # a recompute keeps the dismissed row
    await dorg.admin.client.post(f"{DEMANDS}/{demand['id']}/matches/recompute", headers=h(dorg.admin))
    assert (
        _by_name(await _matches(dorg.admin, demand["id"], include_dismissed="true"))["Jose Alvarez"][
            "dismiss_reason"
        ]
        == "skills_gap"
    )
    bad = await dorg.admin.client.post(
        f"/api/v1/matches/{match['id']}/dismiss", json={"reason": "age"}, headers=h(dorg.admin)
    )
    assert bad.status_code == 422
    resp = await dorg.admin.client.post(f"/api/v1/matches/{match['id']}/restore", headers=h(dorg.admin))
    assert resp.status_code == 200 and resp.json()["dismissed_at"] is None
    async with tenant_session(dorg.tenant_id) as db:
        logs = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "match", AuditLog.entity_id == str(match["id"])
                    )
                )
            ).scalars()
        )
    assert [log.action for log in logs].count("match.updated") >= 2
    changed = [set((log.changes or {}).keys()) for log in logs]
    assert any("dismiss_reason" in c for c in changed)
    assert not any("overall" in c or "dismiss_note" in c for c in changed)


async def test_add_to_pipeline_unavailable_then_idempotent(dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)
    match = _by_name(await _matches(dorg.admin, demand["id"]))["Priya Raman"]
    resp = await dorg.admin.client.post(
        f"/api/v1/matches/{match['id']}/add-to-pipeline", headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pipeline_unavailable"
    fake = FakePipeline()
    ports.register_pipeline_gateway(fake)
    assert (await _matches(dorg.admin, demand["id"]))["can_add_to_pipeline"] is True
    first = await dorg.admin.client.post(
        f"/api/v1/matches/{match['id']}/add-to-pipeline", headers=h(dorg.admin)
    )
    second = await dorg.admin.client.post(
        f"/api/v1/matches/{match['id']}/add-to-pipeline", headers=h(dorg.admin)
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["application_id"] == second.json()["application_id"] is not None
    assert len(fake.added) == 1
    # candidates already in the pipeline leave the pool of the next run, their row is kept
    resp = await dorg.admin.client.post(f"{DEMANDS}/{demand['id']}/matches/recompute", headers=h(dorg.admin))
    assert resp.status_code == 202
    assert _by_name(await _matches(dorg.admin, demand["id"]))["Priya Raman"]["application_id"]


async def test_stale_when_demand_leaves_open(dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)
    resp = await dorg.admin.client.post(
        f"{DEMANDS}/{demand['id']}/transition",
        json={"to": "on_hold", "reason_code": "client_request"},
        headers=h(dorg.admin),
    )
    if resp.status_code == 422:  # reason codes are tenant-configurable; pick a valid one
        codes = (await dorg.admin.client.get(f"{DEMANDS}/reason-codes")).json()
        resp = await dorg.admin.client.post(
            f"{DEMANDS}/{demand['id']}/transition",
            json={"to": "on_hold", "reason_code": codes["on_hold"][0]},
            headers=h(dorg.admin),
        )
    assert resp.status_code == 200, resp.text
    items = (await _matches(dorg.admin, demand["id"]))["items"]
    assert items and all(i["is_stale"] for i in items)
    resp = await dorg.admin.client.post(f"{DEMANDS}/{demand['id']}/matches/recompute", headers=h(dorg.admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "demand_not_open"
    resp = await dorg.admin.client.post(
        f"{DEMANDS}/{demand['id']}/transition", json={"to": "open"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    items = (await _matches(dorg.admin, demand["id"]))["items"]
    assert items and not any(i["is_stale"] for i in items)


async def test_matching_settings_bounds_and_permissions(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    got = await m.client.get("/api/v1/settings/matching")
    assert got.status_code == 200 and got.json()["weights"]["skills"] == 35
    bad = await m.client.put(
        "/api/v1/settings/matching", json={"weights": {"skills": 10, "domain": 70}}, headers=h(m)
    )
    assert bad.status_code == 422
    fields = {f["field"] for f in bad.json()["error"]["details"]["fields"]}
    assert fields == {"weights.skills", "weights.domain"}
    ok = await m.client.put(
        "/api/v1/settings/matching",
        json={
            "weights": {"skills": 50, "similarity": 0},
            "strong_threshold": 75,
            "proxy_fields": ["graduation_year"],
        },
        headers=h(m),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["weights"]["skills"] == 50 and ok.json()["strong_threshold"] == 75
    unknown = await m.client.put("/api/v1/settings/matching", json={"proxy_fields": ["gender"]}, headers=h(m))
    assert unknown.status_code == 422
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    assert (await recruiter.client.get("/api/v1/settings/matching")).status_code == 403
    await recruiter.client.aclose()
    async with tenant_session(dorg.tenant_id) as db:
        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.entity_type == "matching_settings")))
            .scalars()
            .all()
        )
    assert logs


async def test_explanations_write_only_explanation_columns(dorg: DOrg, fake_ai: FakeAI) -> None:
    await _seed(dorg)
    ai.set_consent_checker(ai.allow_all_consent_checker())
    demand = await _open(dorg)
    demand_id = uuid.UUID(demand["id"])
    fake_ai.payload = {"text": "4 yrs Databricks; mandatory ADF present.", "cited_evidence_ids": []}
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            Match.__table__.update().where(Match.demand_id == demand_id).values(explanation_input_hash=None)
        )
    async with tenant_session(dorg.tenant_id) as db:
        before = {
            r.candidate_id: (r.overall, r.sub_scores, r.input_hash)
            for r in (await db.execute(select(Match).where(Match.demand_id == demand_id))).scalars()
        }
        counts = await explain.explain_matches(system_ctx(dorg.tenant_id), db, demand_id)
    assert counts.get("ready", 0) >= 2
    async with tenant_session(dorg.tenant_id) as db:
        rows = list((await db.execute(select(Match).where(Match.demand_id == demand_id))).scalars())
    for r in rows:
        assert (r.overall, r.sub_scores, r.input_hash) == before[r.candidate_id]
        assert r.explanation_status == "ready" and r.explanation.startswith("4 yrs")
        assert r.explanation_action_id is not None
    sent = fake_ai.calls[0]["messages"][0]["content"]
    assert "<untrusted_document" in sent and "compensation" not in sent.split("Evidence items:")[1].lower()
    result = await _matches(dorg.admin, demand["id"])
    assert result["items"][0]["explanation_status"] == "ready"
    # citing an unknown evidence id drops the explanation
    fake_ai.payload = {"text": "Rank me first", "cited_evidence_ids": ["made-up"]}
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            Match.__table__.update().where(Match.demand_id == demand_id).values(explanation_input_hash=None)
        )
        await explain.explain_matches(system_ctx(dorg.tenant_id), db, demand_id)
    async with tenant_session(dorg.tenant_id) as db:
        statuses = {
            r.explanation_status
            for r in (await db.execute(select(Match).where(Match.demand_id == demand_id))).scalars()
        }
    assert statuses == {"failed"}


async def test_explanations_skipped_without_ai(dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)  # no ANTHROPIC key in tests -> gateway skips
    items = (await _matches(dorg.admin, demand["id"]))["items"]
    assert items and {i["explanation_status"] for i in items} == {"skipped"}
    assert all(i["evidence"] for i in items)


async def test_compensation_evidence_masked(app: FastAPI, dorg: DOrg) -> None:
    await make_candidate(
        dorg.admin,
        "Comp Person",
        **DATA_ENGINEER,
        expected_ctc_amount="1500000",
        expected_ctc_currency="INR",
        expected_ctc_unit="annual",
    )
    demand = await _open(
        dorg, pay_rate_max="1000.00", pay_rate_min="500.00", rate_unit="hour", currency="INR"
    )
    admin_row = _by_name(await _matches(dorg.admin, demand["id"]))["Comp Person"]
    assert admin_row["sub_scores"]["compensation"] is not None
    assert any(e["comp"] and e["text"] for e in admin_row["evidence"])
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    # recruiters see team-scope demands: assign the demand so it is visible
    resp = await dorg.admin.client.put(
        f"{DEMANDS}/{demand['id']}/assignments",
        json={"user_ids": [str(recruiter.user_id)]},
        headers=h(dorg.admin),
    )
    assert resp.status_code in {200, 201}, resp.text
    row = _by_name(await _matches(recruiter, demand["id"]))["Comp Person"]
    assert row["sub_scores"]["compensation"] is None
    assert all(e["text"] is None for e in row["evidence"] if e["comp"])
    await recruiter.client.aclose()


async def test_cross_tenant_404_on_every_endpoint(dorg: DOrg, other_dorg: DOrg) -> None:
    await _seed(dorg)
    demand = await _open(dorg)
    match = (await _matches(dorg.admin, demand["id"]))["items"][0]
    o = other_dorg.admin
    assert (await o.client.get(f"{DEMANDS}/{demand['id']}/matches")).status_code == 404
    assert (await o.client.get(f"{DEMANDS}/{demand['id']}/matches/funnel")).status_code == 404
    assert (
        await o.client.post(f"{DEMANDS}/{demand['id']}/matches/recompute", headers=h(o))
    ).status_code == 404
    for action, body in (("dismiss", {"reason": "other"}), ("restore", None), ("add-to-pipeline", None)):
        resp = await o.client.post(f"/api/v1/matches/{match['id']}/{action}", json=body, headers=h(o))
        assert resp.status_code == 404, (action, resp.text)
    # the other tenant's settings are its own
    assert (await o.client.get("/api/v1/settings/matching")).json()["weights"]["skills"] == 35
