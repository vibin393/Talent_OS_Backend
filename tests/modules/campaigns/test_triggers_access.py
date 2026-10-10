# ruff: noqa: E501
"""Epic 39 story 39.6 (FR-200) + isolation / roles / erasure / retention / catalogue registrations."""

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import subscribe
from app.models.campaigns import CampaignMessage, Enrollment
from app.models.candidates import Candidate
from app.services.campaigns import sequences, triggers
from app.services.campaigns.events import CandidateDormant
from tests.epic5 import h
from tests.modules.campaigns.helpers import API, audience, candidate, email_campaign, insert_message, setup_marketing
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import join


async def _published(m: Any, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    resp = await m.client.post(f"{API}/sequences", json={"name": "Re-engage", "steps": steps or [{"kind": "wait", "config": {"days": 3}}]}, headers=h(m))
    return dict((await m.client.post(f"{API}/sequences/{resp.json()['id']}/publish", headers=h(m))).json())


def _action_ctx(db: Any, tenant_id: uuid.UUID, rule_id: uuid.UUID | None, cid: uuid.UUID, seq_id: str, cap: int) -> Any:
    from app.services.workflow.actions import ActionContext

    return ActionContext(
        ctx=RequestContext(tenant_id=tenant_id, actor_kind="workflow", actor_id=rule_id),
        db=db,
        tenant_id=tenant_id,
        rule_id=rule_id,
        rule_created_by=uuid.uuid4(),
        trigger="campaigns.silver_medalist_matched",
        entity_type="candidate",
        entity_id=cid,
        snapshot={},
        params=triggers.EnrolParams(sequence_id=uuid.UUID(seq_id), daily_cap=cap),
    )


async def test_enrol_action_respects_the_daily_cap(dorg: DOrg) -> None:
    from app.services.workflow.actions import ACTIONS

    m = dorg.admin
    seq = await _published(m)
    cands = [await candidate(m, f"Silver {i}") for i in range(5)]
    rule_id = uuid.uuid4()
    results = []
    for cid in cands:
        async with tenant_session(dorg.tenant_id) as db:
            results.append(await ACTIONS[triggers.ACTION_KEY].run(_action_ctx(db, dorg.tenant_id, rule_id, cid, seq["id"], 2)))
    assert results.count("skipped: daily_cap") == 3
    assert sum(1 for r in results if r.startswith("Enrolled 1")) == 2
    async with tenant_session(dorg.tenant_id) as db:
        rows = list((await db.execute(select(Enrollment).where(Enrollment.sequence_id == uuid.UUID(seq["id"])))).scalars())
    assert len(rows) == 2 and {r.origin for r in rows} == {"workflow"} and {r.workflow_rule_id for r in rows} == {rule_id}


async def test_dormant_detection_fires_once_per_crossing(dorg: DOrg) -> None:
    m = dorg.admin
    sleepy = await candidate(m, "Sleepy")
    await candidate(m, "Active")
    today = date(2026, 10, 7)
    async with tenant_session(dorg.tenant_id) as db:
        target = triggers._months_ago(today, 6)
        from datetime import UTC, datetime

        await db.execute(update(Candidate).where(Candidate.id == sleepy).values(updated_at=datetime.combine(target, datetime.min.time(), tzinfo=UTC) + timedelta(hours=6)))
    seen: list[CandidateDormant] = []

    def on(e: CandidateDormant) -> None:
        if e.tenant_id == dorg.tenant_id:
            seen.append(e)

    on.__name__ = "test_campaigns_dormant_probe"
    subscribe(CandidateDormant, on)
    async with tenant_session(dorg.tenant_id) as db:
        assert await triggers.detect_dormant(db, dorg.tenant_id, today) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await triggers.detect_dormant(db, dorg.tenant_id, today) == 0  # once per day
    assert [(e.candidate_id, e.months) for e in seen] == [(sleepy, 6)]


async def test_silver_medalists_matched_emits_for_pool_members_above_seventy(dorg: DOrg) -> None:
    from tests.modules.demands.helpers import open_demand
    from tests.modules.talent_pools.helpers import add_match

    m = dorg.admin
    medal = await candidate(m, "Medal")
    low = await candidate(m, "Low")
    demand = await open_demand(m, dorg.client_id)
    resp = await m.client.post("/api/v1/talent-pools", json={"name": "Silver Medalists"}, headers=h(m))
    pool = resp.json()
    await m.client.post(f"/api/v1/talent-pools/{pool['id']}/members", json={"candidate_ids": [str(medal), str(low)]}, headers=h(m))
    async with tenant_session(dorg.tenant_id) as db:
        from app.models.talent_pools import TalentPool

        await db.execute(update(TalentPool).where(TalentPool.id == uuid.UUID(pool["id"])).values(template_key="silver_medalists"))
    await add_match(dorg.tenant_id, demand["id"], medal, "82")
    await add_match(dorg.tenant_id, demand["id"], low, "40")
    async with tenant_session(dorg.tenant_id) as db:
        assert await triggers.silver_medalists_matched(db, dorg.tenant_id, uuid.UUID(demand["id"])) == 1


async def test_registrations_rediscovery_and_quick_search(dorg: DOrg) -> None:
    from app.services import campaigns_service
    from app.services.approvals.registry import subject
    from app.services.matching.rediscovery import sections
    from app.services.notifications_service import NOTIFICATION_TYPES
    from app.services.privacy.retention_classes import retention_bindings
    from app.services.workflow.registry import TRIGGERS
    from tests.epic5 import system_ctx

    assert "sequences" in sections()
    assert subject("campaign") is not None and subject("campaign").permission == "campaign:approve"
    for key in ("campaigns.candidate_dormant", "campaigns.silver_medalist_matched", "campaigns.pool_members_added"):
        assert key in TRIGGERS
    for key in ("campaign_approval_requested", "campaign_completed", "sequence_reply", "campaign_paused", "marketing_tenant_paused"):
        assert key in NOTIFICATION_TYPES
    bindings = retention_bindings()
    assert bindings["campaign_messages"].class_key == "marketing_logs" and bindings["engagement_events"].class_key == "marketing_logs"
    m = dorg.admin
    cid = await candidate(m, "Search")
    await email_campaign(m, (await audience(m, [cid]))["id"], name="Diwali hiring drive")
    async with tenant_session(dorg.tenant_id) as db:
        hits = await campaigns_service.quick_search(system_ctx(dorg.tenant_id, m.user_id), db, "diwali", 5)
    assert [x.title for x in hits] == ["Diwali hiring drive"] and hits[0].type == "campaign"


async def test_cross_tenant_404_unauthenticated_401_and_marketer_role(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    m = dorg.admin
    cid = await candidate(m, "Mine")
    aud = await audience(m, [cid])
    camp = await email_campaign(m, aud["id"])
    o = other_dorg.admin
    for path in (f"{API}/audiences/{aud['id']}", f"{API}/campaigns/{camp['id']}", f"{API}/campaigns/{camp['id']}/analytics"):
        assert (await o.client.get(path)).status_code == 404, path
    from tests.modules.identity.conftest import new_client

    anon = new_client(app)
    assert (await anon.get(f"{API}/campaigns")).status_code == 401
    await anon.aclose()
    marketer = await join(app, dorg.tenant, "recruitment_marketer", name="Mia Marketer")
    assert (await marketer.client.get(f"{API}/campaigns")).status_code == 200
    created = await marketer.client.post(f"{API}/audiences", json={"name": "Mine"}, headers=h(marketer))
    assert created.status_code == 201
    assert (await marketer.client.patch(f"{API}/settings", json={"approval_threshold": 5}, headers=h(marketer))).status_code == 403
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rob Recruiter")
    assert (await recruiter.client.get(f"{API}/campaigns")).status_code == 200
    assert (await recruiter.client.post(f"{API}/audiences", json={"name": "x"}, headers=h(recruiter))).status_code == 403


async def test_erasure_handler_covers_marketing_tables(dorg: DOrg) -> None:
    from app.services import load_erasure_handlers
    from app.services.campaigns_erasure import CampaignsMarketingHandler
    from app.services.privacy.registry import ErasureContext, claimed_tables

    load_erasure_handlers()
    for table in ("audience_list_entries", "audience_members", "campaign_messages", "engagement_events", "campaign_attributions", "enrollments"):
        assert table in claimed_tables()
    m = dorg.admin
    await setup_marketing(m)
    cid = await candidate(m, "Erase me")
    aud = await audience(m, [cid])
    camp = uuid.UUID((await email_campaign(m, aud["id"]))["id"])
    await insert_message(dorg.tenant_id, cid, campaign_id=camp)
    seq = await _published(m)
    async with tenant_session(dorg.tenant_id) as db:
        from tests.epic5 import system_ctx

        await sequences.enrol(system_ctx(dorg.tenant_id, m.user_id), db, uuid.UUID(seq["id"]), [cid])
    handler = CampaignsMarketingHandler()
    async with tenant_session(dorg.tenant_id) as db:
        ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
        exported = await handler.export(db, ctx)
        assert exported is not None and exported["campaign_messages"][0]["campaign"] == "Spring hiring"
        counts = await handler.erase(db, ctx)
        assert counts["campaign_messages"] == 1 and counts["enrollments"] == 1 and counts["audience_list_entries"] == 1
        assert await handler.verify(db, ctx) == []
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(select(CampaignMessage).where(CampaignMessage.candidate_id == cid))).first() is None
