# ruff: noqa: E501
"""Epic 49 A5/A7/A9/A11: adverse-impact report on queue ``analytics`` end to end, auditor pack (PDF + CSV), public
summary, principals, erasure / DSR export without values."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import delete, text

from app.core.database import tenant_session
from app.models.fairness import DemographicSelfId
from app.models.matching import Match
from app.services.fairness import categories, crypto, self_id
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.fairness.helpers import F, enable, make_candidate
from tests.p2 import sign_in_candidate


async def seed_answers(tenant_id: uuid.UUID, answers: dict[uuid.UUID, dict[str, str]]) -> None:
    async with tenant_session(tenant_id) as db:
        cset = await categories.active_set(db)
        await self_id.access(db, "apply_write")
        for cid, a in answers.items():
            db.add(
                DemographicSelfId(
                    id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=cid, set_id=cset.id, set_version=cset.version,
                    answers=crypto.seal_answers(tenant_id, a), source="portal",
                )
            )
        await db.flush()


async def seed_matches(tenant_id: uuid.UUID, demand_id: uuid.UUID, scores: dict[uuid.UUID, int]) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(delete(Match).where(Match.demand_id == demand_id))
        for cid, score in scores.items():
            db.add(
                Match(
                    id=uuid.uuid4(), tenant_id=tenant_id, demand_id=demand_id, candidate_id=cid, overall=Decimal(score),
                    sub_scores={"skills": score}, scoring_version="v-test", input_hash="0" * 64,
                    computed_at=datetime.now(UTC), explanation="because", explanation_status="ready",
                )
            )


async def _population(org: DOrg) -> tuple[uuid.UUID, dict[uuid.UUID, int]]:
    demand = await open_demand(org.admin, org.client_id)
    did = uuid.UUID(demand["id"])
    answers: dict[uuid.UUID, dict[str, str]] = {}
    scores: dict[uuid.UUID, int] = {}
    for i in range(12):  # women: 6 of 12 >= 70
        cid = await make_candidate(org, f"W {i}")
        answers[cid] = {"gender": "woman", "age_band": "25_34" if i % 2 else "35_44"}
        scores[cid] = 80 if i < 6 else 40
    for i in range(12):  # men: 7 of 12 >= 70
        cid = await make_candidate(org, f"M {i}")
        answers[cid] = {"gender": "man", "age_band": "25_34" if i % 2 else "35_44", "disability": "prefer_not_to_say"}
        scores[cid] = 85 if i < 7 else 30
    for i in range(3):  # no self-ID -> unknown
        cid = await make_candidate(org, f"U {i}")
        scores[cid] = 75
    await seed_answers(org.tenant_id, answers)
    await seed_matches(org.tenant_id, did, scores)
    return did, scores


def _rows(run: dict, key: str) -> dict[str, dict]:
    table = next(t for t in run["snapshot"]["tables"] if t["key"] == key)
    return {r["category"]: r for r in table["rows"]}


async def test_ai_match_report_pack_and_publication(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    await enable(dorg)
    did, _scores = await _population(dorg)
    today = date.today()
    body = {
        "selection_point": "ai_match_threshold", "params": {"min_score": 70},
        "date_from": str(today - timedelta(days=365)), "date_to": str(today),
        "filters": {"demand_ids": [str(did)]}, "intersections": ["gender×age_band"],
    }
    r = await dorg.admin.client.post(f"{F}/runs", json=body, headers=h(dorg.admin))
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    got = await dorg.admin.client.get(f"{F}/runs/{run_id}")
    assert got.status_code == 200, got.text
    run = got.json()
    assert run["status"] == "completed", run
    assert run["total_applicants"] == 27 and run["method"] == "both"
    g = _rows(run, "gender")
    assert g["woman"]["applicants"] == 12 and g["woman"]["selected"] == 6 and g["woman"]["rate"] == "0.5000"
    assert g["man"]["applicants"] == 12 and g["man"]["selected"] == 7
    assert g["woman"]["impact_ratio"] == "0.8571" and g["woman"]["flag"] is False
    assert g["unknown"]["applicants"] == 3 and not g["unknown"]["suppressed"]
    assert run["unknown_counts"]["gender"] == 3
    assert "scoring_rate" in g["woman"]
    inter = _rows(run, "gender×age_band")
    assert inter["woman|25_34"]["suppressed"] or inter["woman|25_34"]["applicants"] == 6
    import json

    blob = json.dumps(run["snapshot"])
    async with tenant_session(dorg.tenant_id) as db:
        ids = [str(x) for x in (await db.execute(text("SELECT id FROM candidates"))).scalars()]
        acts = set((await db.execute(text("SELECT action FROM audit_logs WHERE entity_type = 'fairness_run'"))).scalars())
    assert not any(i in blob for i in ids)
    assert {"fairness.run_requested", "fairness.run_viewed"} <= acts

    # listing
    lst = await dorg.admin.client.get(f"{F}/runs")
    assert lst.status_code == 200 and lst.json()[0]["id"] == run_id

    # AEDT inventory seeded inactive; auditor pack
    inv = await dorg.admin.client.get(f"{F}/aedt")
    assert inv.status_code == 200, inv.text
    entries = {e["feature"]: e for e in inv.json()}
    assert {"ai_match", "ai_screening", "assessment_autoscore"} <= set(entries)
    assert all(e["status"] == "inactive" for e in entries.values())
    assert entries["ai_match"]["version"] == "v-test"
    aedt_id = entries["ai_match"]["id"]
    act = await dorg.admin.client.patch(f"{F}/aedt/{aedt_id}", json={"status": "active"}, headers=h(dorg.admin))
    assert act.status_code == 200 and act.json()["status"] == "active"
    p = await dorg.admin.client.post(f"{F}/runs/{run_id}/pack", json={"aedt_id": aedt_id}, headers=h(dorg.admin))
    assert p.status_code == 200, p.text
    files = {f["name"]: f for f in p.json()["files"]}
    assert "auditor-pack.pdf" in files and any(n.endswith(".csv") for n in files)
    pdf = await dorg.admin.client.get(files["auditor-pack.pdf"]["url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
    from app.services.documents.render import pdf_text

    txt = " ".join(pdf_text(pdf.content).split())
    assert "Race/ethnicity is not collected" in txt and "four-fifths" in txt and "AI Match" in txt
    csv_name = next(n for n in files if n.startswith("gender") and n.endswith(".csv"))
    csv = await dorg.admin.client.get(files[csv_name]["url"])
    assert csv.status_code == 200 and "Impact ratio" in csv.text
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'fairness.pack_exported'"))).scalar_one() == 1

    # publication -> Career Portal summary (suppressed cells stay suppressed)
    site = await dorg.admin.client.patch("/api/v1/admin/career-site", json={"enabled": True}, headers=h(dorg.admin))
    assert site.status_code == 200, site.text
    pub = await dorg.admin.client.post(
        f"{F}/publications", json={"run_id": run_id, "aedt_id": aedt_id, "audit_date": str(today), "distribution_date": str(today)}, headers=h(dorg.admin)
    )
    assert pub.status_code == 201, pub.text
    summary = await dorg.admin.client.get(f"/api/v1/public/fairness/{dorg.tenant.slug}/summary")
    assert summary.status_code == 200, summary.text
    assert len(summary.json()) == 1 and summary.json()[0]["summary"]["unknown_counts"]["gender"] == 3
    un = await dorg.admin.client.post(f"{F}/publications/{pub.json()['id']}/unpublish", headers=h(dorg.admin))
    assert un.status_code == 200
    assert (await dorg.admin.client.get(f"/api/v1/public/fairness/{dorg.tenant.slug}/summary")).json() == []

    # cross-tenant -> 404; candidate cookie -> 401
    assert (await other_dorg.admin.client.get(f"{F}/runs/{run_id}")).status_code == 404
    cid = await make_candidate(dorg)
    cand = await sign_in_candidate(app, cid, dorg.tenant_id)
    try:
        assert (await cand.get(f"{F}/runs")).status_code == 401
        assert (await cand.get(f"{F}/settings")).status_code == 401
    finally:
        await cand.aclose()


async def test_permissions(app: FastAPI, dorg: DOrg) -> None:
    from tests.modules.identity.conftest import join

    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        assert (await recruiter.client.get(f"{F}/runs")).status_code == 403
        assert (await recruiter.client.patch(f"{F}/settings", json={"k": 6}, headers=h(recruiter))).status_code == 403
    finally:
        await recruiter.client.aclose()
    from app.core.permissions import DEFAULT_ROLES

    compliance = next(r for r in DEFAULT_ROLES if r.key == "compliance")
    assert {"fairness:read", "fairness:configure", "aedt:manage"} <= set(compliance.permissions)


async def test_erasure_and_dsr_export_without_values(dorg: DOrg) -> None:
    from app.services.fairness_erasure import FairnessHandler
    from app.services.privacy.registry import ErasureContext

    cid = await make_candidate(dorg)
    await seed_answers(dorg.tenant_id, {cid: {"gender": "woman", "disability": "yes"}})
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    handler = FairnessHandler()
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
    assert exported == {"self_id": "on file; view or change it in your Candidate Portal"}
    async with tenant_session(dorg.tenant_id) as db:
        counts = await handler.erase(db, ctx)
    assert counts["self_ids"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
        assert await handler.export(db, ctx) is None
