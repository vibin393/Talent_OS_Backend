"""Bench-first matching: bench section, consent, ranking, strong-match notices, reviews, workflow
condition."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import emit
from app.events.matching import MatchRunCompleted
from app.services.workflow.registry import evaluate_condition
from tests.modules.bench.helpers import BENCH, end_placement, grant_consent, placement, put_match
from tests.modules.demands.helpers import DOrg, admin_ctx, h, open_demand


async def _bench_worker(org: DOrg, days_ago: int = 3, **kw: object) -> dict:  # type: ignore[type-arg]
    p = await placement(org, **kw)  # type: ignore[arg-type]
    await end_placement(org, p, date.today() - timedelta(days=days_ago))
    await grant_consent(org, p["candidate_id"])
    return p


async def _notices(org: DOrg) -> int:
    async with tenant_session(org.tenant_id) as db:
        return int((await db.execute(text("SELECT count(*) FROM bench_match_notices"))).scalar_one())


async def _notifications(org: DOrg, demand_id: str) -> int:
    async with tenant_session(org.tenant_id) as db:
        return int(
            (
                await db.execute(
                    text(
                        "SELECT count(*) FROM notifications "
                        "WHERE type = 'bench.strong_match' AND entity_id = :d"
                    ),
                    {"d": uuid.UUID(demand_id)},
                )
            ).scalar_one()
        )


async def _run_completed(org: DOrg, demand_id: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        emit(
            db,
            MatchRunCompleted(
                tenant_id=org.tenant_id,
                demand_id=uuid.UUID(demand_id),
                run_id=uuid.uuid4(),
                status="completed",
            ),
        )


async def test_bench_section_pool_consent_ranking_and_reviews(
    dorg: DOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = dorg.admin
    d = await open_demand(m, dorg.client_id)
    did = d["id"]
    a = await _bench_worker(dorg, days_ago=3)
    b = await _bench_worker(dorg, days_ago=10)
    c = await _bench_worker(dorg, days_ago=1)
    rolling = await placement(dorg, planned_end=date.today() + timedelta(days=10))
    await grant_consent(dorg, rolling["candidate_id"])
    nc = await placement(dorg)  # no consent
    await end_placement(dorg, nc, date.today() - timedelta(days=2))
    from app.services.candidates import search_port

    real = search_port.ai_consent_active

    async def without_nc(db: object, ids: object) -> set[uuid.UUID]:
        return {i for i in await real(db, ids) if str(i) != nc["candidate_id"]}  # type: ignore[arg-type]

    monkeypatch.setattr(search_port, "ai_consent_active", without_nc)
    outsider = await placement(dorg)  # active, not rolling off: not in the pool
    await grant_consent(dorg, outsider["candidate_id"])
    for p, score in ((a, "82.0"), (b, "82.0"), (c, "40.0"), (rolling, "75.0"), (outsider, "99.0")):
        await put_match(dorg, did, p["candidate_id"], score)

    async with tenant_session(dorg.tenant_id) as db:  # rows scored before consent was withdrawn
        await db.execute(
            text("DELETE FROM matches WHERE candidate_id = :c"), {"c": uuid.UUID(nc["candidate_id"])}
        )
    resp = await m.client.get(f"{BENCH}/demands/{did}/matches")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [i["candidate"]["id"] for i in body["items"]]
    assert outsider["candidate_id"] not in ids
    # ties broken by bench days desc (b has more), then id
    assert ids[:4] == [b["candidate_id"], a["candidate_id"], rolling["candidate_id"], c["candidate_id"]]
    by = {i["candidate"]["id"]: i for i in body["items"]}
    assert (
        by[rolling["candidate_id"]]["pool"] == "rolling_off"
        and by[rolling["candidate_id"]]["rolling_off_end_date"]
    )
    assert by[nc["candidate_id"]]["score_unavailable_reason"] == "no_consent"
    assert by[nc["candidate_id"]]["overall"] is None and by[nc["candidate_id"]]["score_pending"] is False
    strong = {cid for cid, i in by.items() if i["strong"]}
    assert strong == {a["candidate_id"], b["candidate_id"], rolling["candidate_id"]}
    assert body["unreviewed_strong"] == 3

    # workflow condition false while strong matches exist and the demand is not marked reviewed
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        assert await evaluate_condition(ctx, db, "bench.matches_reviewed", demand_id=uuid.UUID(did)) is False

    refused = await m.client.post(f"{BENCH}/demands/{did}/reviews/complete", headers=h(m))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "bench_matches_unreviewed"
    assert len(refused.json()["error"]["details"]["candidate_ids"]) == 3

    no_reason = await m.client.post(
        f"{BENCH}/demands/{did}/reviews",
        json={"candidate_id": a["candidate_id"], "decision": "dismissed"},
        headers=h(m),
    )
    assert no_reason.status_code == 422
    for p, decision in ((a, "reviewed"), (b, "dismissed"), (rolling, "reviewed")):
        resp = await m.client.post(
            f"{BENCH}/demands/{did}/reviews",
            json={"candidate_id": p["candidate_id"], "decision": decision, "reason": "Rate too high"},
            headers=h(m),
        )
        assert resp.status_code == 200, resp.text
    done = await m.client.post(f"{BENCH}/demands/{did}/reviews/complete", headers=h(m))
    assert done.status_code == 200, done.text
    async with tenant_session(dorg.tenant_id) as db:
        assert await evaluate_condition(ctx, db, "bench.matches_reviewed", demand_id=uuid.UUID(did)) is True
    after = (await m.client.get(f"{BENCH}/demands/{did}/matches")).json()
    assert after["reviewed_complete"] is True and after["unreviewed_strong"] == 0


async def test_condition_true_without_strong_matches_and_demand_not_open(dorg: DOrg) -> None:
    m = dorg.admin
    d = await open_demand(m, dorg.client_id)
    w = await _bench_worker(dorg)
    await put_match(dorg, d["id"], w["candidate_id"], "30.0")
    async with tenant_session(dorg.tenant_id) as db:
        assert await evaluate_condition(
            admin_ctx(dorg), db, "bench.matches_reviewed", demand_id=uuid.UUID(d["id"])
        )
    from tests.modules.demands.helpers import create_demand

    draft = await create_demand(m, dorg.client_id)
    resp = await m.client.get(f"{BENCH}/demands/{draft['id']}/matches")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "demand_not_open"


async def test_strong_match_notified_once_per_pair(dorg: DOrg) -> None:
    m = dorg.admin
    d = await open_demand(m, dorg.client_id)
    assign = await m.client.put(
        f"/api/v1/demands/{d['id']}/assignments", json={"user_ids": [str(m.user_id)]}, headers=h(m)
    )
    assert assign.status_code == 200, assign.text
    w = await _bench_worker(dorg)
    await put_match(dorg, d["id"], w["candidate_id"], "90.0")
    await _run_completed(dorg, d["id"])
    await _run_completed(dorg, d["id"])
    assert await _notices(dorg) == 1
    assert await _notifications(dorg, d["id"]) == 1

    # concurrent scans -> still one notice per pair
    from app.services.placements import bench_matching

    ctx = RequestContext(tenant_id=dorg.tenant_id, actor_kind="system")
    w2 = await _bench_worker(dorg)
    await put_match(dorg, d["id"], w2["candidate_id"], "91.0")

    async def scan() -> None:
        async with tenant_session(dorg.tenant_id) as db:
            await bench_matching.notify_strong(ctx, db, demand_ids=[uuid.UUID(d["id"])])

    await asyncio.gather(*(scan() for _ in range(8)))
    assert await _notices(dorg) == 2
