"""Epic 38: P1 saved lists as Pools (decision 11), digest, workflow action, erasure."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import tenant_session
from app.models.candidates import CandidateList, CandidateListMember, CandidateListShare
from app.models.notifications import Notification
from app.models.talent_pools import TalentPool, TalentPoolMember, TalentPoolShare, TalentPoolSubscription
from app.services.privacy.registry import ErasureContext
from app.services.talent_pools import digest
from app.services.talent_pools_erasure import TalentPoolMembersHandler
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg
from tests.modules.talent_pools.helpers import LISTS, POOLS, active_member_ids, candidate, create_pool, dynamic_pool

API = "/api/v1"
MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "20261006_302_02_pools.py"


async def test_alias_create_keeps_p1_shape_and_mirrors_pool_members(dorg: DOrg) -> None:
    a = await candidate(dorg.admin, "Asha")
    resp = await dorg.admin.client.post(LISTS, json={"name": "Java folks"}, headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    lst = resp.json()
    assert set(lst) >= {"id", "member_count", "shared_user_ids", "is_owner"}
    async with tenant_session(dorg.tenant_id) as db:
        pool = (await db.execute(select(TalentPool).where(TalentPool.legacy_list_id == uuid.UUID(lst["id"])))).scalar_one()
    assert (pool.kind, pool.visibility, pool.name) == ("static", "private", "Java folks")

    # a member added through the Pool API mirrors down to the P1 table
    add = await dorg.admin.client.post(f"{POOLS}/{pool.id}/members", json={"candidate_ids": [str(a)]}, headers=h(dorg.admin))
    assert add.status_code == 200, add.text
    async with tenant_session(dorg.tenant_id) as db:
        p1 = (await db.execute(select(CandidateListMember.candidate_id).where(CandidateListMember.list_id == uuid.UUID(lst["id"])))).scalars().all()
    assert p1 == [a]
    got = (await dorg.admin.client.get(f"{LISTS}/{lst['id']}")).json()
    assert got["member_count"] == 1
    listed = (await dorg.admin.client.get(LISTS)).json()
    assert [i["name"] for i in listed["items"]] == ["Java folks"]

    # alias writes go to the Pool and down to P1
    renamed = await dorg.admin.client.patch(f"{LISTS}/{lst['id']}", json={"name": "Java people"}, headers=h(dorg.admin))
    assert renamed.json()["name"] == "Java people"
    assert (await dorg.admin.client.get(f"{POOLS}/{pool.id}")).json()["name"] == "Java people"
    removed = await dorg.admin.client.delete(f"{LISTS}/{lst['id']}/members/{a}", headers=h(dorg.admin))
    assert removed.status_code == 200 and removed.json()["member_count"] == 0
    assert await active_member_ids(dorg.tenant_id, pool.id) == set()
    archived = await dorg.admin.client.delete(f"{LISTS}/{lst['id']}", headers=h(dorg.admin))
    assert archived.json()["archived_at"] is not None
    assert (await dorg.admin.client.get(f"{POOLS}/{pool.id}")).json()["status"] == "archived"


async def test_migration_data_step_copies_lists_members_and_shares(dorg: DOrg, owner_db: AsyncEngine) -> None:
    a = await candidate(dorg.admin, "Asha")
    list_id = uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:  # a P1 list written behind the alias's back
        db.add(CandidateList(id=list_id, tenant_id=dorg.tenant_id, name="Old list", owner_id=dorg.admin.user_id))
        await db.flush()
        db.add(CandidateListMember(tenant_id=dorg.tenant_id, list_id=list_id, candidate_id=a, added_by=dorg.admin.user_id))
        db.add(CandidateListShare(tenant_id=dorg.tenant_id, list_id=list_id, user_id=dorg.admin.user_id))
    spec = importlib.util.spec_from_file_location("m302_02", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for _ in range(2):  # idempotent
        async with owner_db.begin() as conn:
            for stmt in (mod._COPY_LISTS, mod._COPY_MEMBERS, mod._COPY_SHARES, mod._COUNT_MEMBERS):
                await conn.execute(text(stmt))
    async with tenant_session(dorg.tenant_id) as db:
        pools = (await db.execute(select(TalentPool).where(TalentPool.legacy_list_id == list_id))).scalars().all()
        assert len(pools) == 1
        pool = pools[0]
        assert (pool.name, pool.member_count, pool.owner_id) == ("Old list", 1, dorg.admin.user_id)
        members = (await db.execute(select(TalentPoolMember).where(TalentPoolMember.pool_id == pool.id))).scalars().all()
        assert [(m.candidate_id, m.source) for m in members] == [(a, "legacy_list")]
        shares = (await db.execute(select(TalentPoolShare).where(TalentPoolShare.pool_id == pool.id))).scalars().all()
        assert [s.user_id for s in shares] == [dorg.admin.user_id]


async def test_digest_one_notification_per_subscriber_and_zero_pools_skipped(dorg: DOrg) -> None:
    a = await candidate(dorg.admin, "Asha")
    busy = await create_pool(dorg.admin, name="Busy")
    quiet = await create_pool(dorg.admin, name="Quiet")
    for p in (busy, quiet):
        assert (await dorg.admin.client.post(f"{POOLS}/{p['id']}/subscription", headers=h(dorg.admin))).json() == {"subscribed": True}
    past = datetime.now(UTC) - timedelta(days=1)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(TalentPoolSubscription).values(last_digest_at=past))
    await dorg.admin.client.post(f"{POOLS}/{busy['id']}/members", json={"candidate_ids": [str(a)]}, headers=h(dorg.admin))
    async with tenant_session(dorg.tenant_id) as db:
        assert await digest.send_digest(db, dorg.tenant_id) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await digest.send_digest(db, dorg.tenant_id) == 0  # nothing new since the last digest
        notes = (await db.execute(select(Notification).where(Notification.type == "pool_new_members"))).scalars().all()
    assert len(notes) == 1 and notes[0].user_id == dorg.admin.user_id
    assert "1 new member in 1 talent pool" in notes[0].title and "Quiet" not in (notes[0].body or "")


async def test_workflow_add_to_pool_refuses_dynamic_pool_at_save(dorg: DOrg) -> None:
    static = await create_pool(dorg.admin, name="Static")
    dynamic = await dynamic_pool(dorg.admin, name="Dynamic")

    def rule(pool_id: str) -> dict[str, object]:
        return {
            "name": "Add new candidates",
            "trigger": "candidate.created",
            "actions": [{"type": "talent_pools.add_to_pool", "params": {"pool_id": pool_id}}],
        }

    bad = await dorg.admin.client.post(f"{API}/workflow/rules", json=rule(dynamic["id"]), headers=h(dorg.admin))
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["details"]["reason"] == "pool_is_dynamic"
    ok = await dorg.admin.client.post(f"{API}/workflow/rules", json=rule(static["id"]), headers=h(dorg.admin))
    assert ok.status_code == 201, ok.text
    assert (await dorg.admin.client.post(f"{API}/workflow/rules/{ok.json()['id']}/activate", headers=h(dorg.admin))).status_code == 200
    cid = await candidate(dorg.admin, "Wanda Workflow")
    assert cid in await active_member_ids(dorg.tenant_id, static["id"])


async def test_erasure_removes_member_rows(dorg: DOrg) -> None:
    a = await candidate(dorg.admin, "Asha")
    pool = await create_pool(dorg.admin, name="P")
    await dorg.admin.client.post(f"{POOLS}/{pool['id']}/members", json={"candidate_ids": [str(a)]}, headers=h(dorg.admin))
    handler = TalentPoolMembersHandler()
    ectx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=a, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ectx)
        assert exported is not None and exported["talent_pools"][0]["pool"] == "P"
        counts = await handler.erase(db, ectx)
        assert counts["members"] == 1
        assert await handler.verify(db, ectx) == []
        p = await db.get(TalentPool, uuid.UUID(pool["id"]))
        assert p is not None and p.member_count == 0
