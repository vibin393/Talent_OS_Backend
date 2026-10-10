"""Epic 38 stories 38.2 / 38.3: static and dynamic Pools, members, scope, refresh, diff (FR-192, FR-194)."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.events import subscribe
from app.core.permissions import Scope
from app.core.semantic import SemanticTimeout
from app.events.talent_pools import PoolMembershipChanged
from app.models.notifications import Notification
from app.models.talent_pools import Segment, TalentPool, TalentPoolRefresh
from app.services import pools_service
from app.services.talent_pools import members as pool_members
from app.services.talent_pools import refresh as pool_refresh
from tests.epic5 import h, patch_candidate
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import join
from tests.modules.talent_pools.helpers import (
    POOLS,
    active_member_ids,
    candidate,
    create_pool,
    ctx_for,
    dynamic_pool,
    pool_row,
    refreshes,
)


async def test_static_pool_crud_and_members(dorg: DOrg) -> None:
    a = await candidate(dorg.admin, "Asha")
    pool = await create_pool(dorg.admin, name="Shortlist")
    assert pool["kind"] == "static" and pool["can_edit"] is True
    resp = await dorg.admin.client.post(
        f"{POOLS}/{pool['id']}/members", json={"candidate_ids": [str(a), str(a)]}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"added": 1, "existing": 0, "member_count": 1}
    again = await dorg.admin.client.post(
        f"{POOLS}/{pool['id']}/members", json={"candidate_ids": [str(a)]}, headers=h(dorg.admin)
    )
    assert again.json()["existing"] == 1
    page = (await dorg.admin.client.get(f"{POOLS}/{pool['id']}/members")).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["source"] == "manual" and item["consent_marketing"] == "none"
    assert item["reason"]["added_by"] == str(dorg.admin.user_id)

    listed = (await dorg.admin.client.get(POOLS, params={"filter": "mine"})).json()
    assert [p["name"] for p in listed] == ["Shortlist"]
    patched = await dorg.admin.client.patch(f"{POOLS}/{pool['id']}", json={"name": "Shortlist 2"}, headers=h(dorg.admin))
    assert patched.json()["name"] == "Shortlist 2"
    removed = await dorg.admin.client.delete(f"{POOLS}/{pool['id']}/members/{a}", headers=h(dorg.admin))
    assert removed.status_code == 204
    assert (await pool_row(dorg.tenant_id, pool["id"])).member_count == 0
    archived = await dorg.admin.client.delete(f"{POOLS}/{pool['id']}", headers=h(dorg.admin))
    assert archived.json()["status"] == "archived"
    assert (await dorg.admin.client.get(POOLS)).json() == []


async def test_dynamic_pool_initial_refresh_reasons_and_manual_add_refused(dorg: DOrg) -> None:
    quick = await candidate(dorg.admin, "Asha Quick", notice_period_days=10)
    await candidate(dorg.admin, "Ben Slow", notice_period_days=90)
    pool = await dynamic_pool(dorg.admin, name="Quick joiners")
    assert await active_member_ids(dorg.tenant_id, pool["id"]) == {quick}
    (first,) = await refreshes(dorg.tenant_id, pool["id"])
    assert (first.trigger, first.status, first.added, first.removed) == ("created", "done", 1, 0)
    detail = (await dorg.admin.client.get(f"{POOLS}/{pool['id']}")).json()
    assert detail["member_count"] == 1 and detail["last_diff"] == {"added": 1, "removed": 0}
    assert detail["segment_name"] == "Quick joiners"
    page = (await dorg.admin.client.get(f"{POOLS}/{pool['id']}/members")).json()
    reason = page["items"][0]["reason"]
    assert reason["matched"][0]["field"] == "notice_period_days"
    assert reason["version"] == 1
    resp = await dorg.admin.client.post(
        f"{POOLS}/{pool['id']}/members", json={"candidate_ids": [str(quick)]}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pool_is_dynamic"


async def test_refresh_diff_event_cooldown_and_dedup(dorg: DOrg) -> None:
    a = await candidate(dorg.admin, "A", notice_period_days=10)
    b = await candidate(dorg.admin, "B", notice_period_days=20)
    c = await candidate(dorg.admin, "C", notice_period_days=99)
    pool = await dynamic_pool(dorg.admin)
    assert await active_member_ids(dorg.tenant_id, pool["id"]) == {a, b}

    seen: list[PoolMembershipChanged] = []

    def spy(e: PoolMembershipChanged) -> None:
        if str(e.pool_id) == pool["id"]:
            seen.append(e)

    subscribe(PoolMembershipChanged, spy)
    await patch_candidate(dorg.admin, str(b), notice_period_days=60)
    await patch_candidate(dorg.admin, str(c), notice_period_days=5)
    resp = await dorg.admin.client.post(f"{POOLS}/{pool['id']}/refresh", headers=h(dorg.admin))
    assert resp.status_code == 202, resp.text
    runs = await refreshes(dorg.tenant_id, pool["id"])
    manual = runs[-1]
    assert (manual.trigger, manual.status, manual.added, manual.removed, manual.evaluated) == ("manual", "done", 1, 1, 2)
    assert await active_member_ids(dorg.tenant_id, pool["id"]) == {a, c}
    assert len(seen) == 1 and (seen[0].added_count, seen[0].removed_count) == (1, 1)

    ctx = ctx_for(dorg.tenant_id, dorg.admin.user_id)
    async with tenant_session(dorg.tenant_id) as db:
        assert await pools_service.member_ids(ctx, db, uuid.UUID(pool["id"]), change="added", refresh_id=manual.id) == [c]
        assert await pools_service.member_ids(ctx, db, uuid.UUID(pool["id"]), change="removed", refresh_id=manual.id) == [b]
        current = await pools_service.member_ids(ctx, db, uuid.UUID(pool["id"]))
        assert current == sorted([a, c])
        assert await pools_service.member_ids(ctx, db, uuid.UUID(pool["id"]), after=current[0]) == current[1:]
        summary = await pools_service.pool_summary(ctx, db, uuid.UUID(pool["id"]))
        assert summary.member_count == 2 and summary.kind == "dynamic"

    too_soon = await dorg.admin.client.post(f"{POOLS}/{pool['id']}/refresh", headers=h(dorg.admin))
    assert too_soon.status_code == 429

    # a refresh that is still running is returned instead of starting a second one
    async with tenant_session(dorg.tenant_id) as db:
        running = TalentPoolRefresh(tenant_id=dorg.tenant_id, pool_id=uuid.UUID(pool["id"]), trigger="nightly", status="running")
        db.add(running)
        await db.flush()
        running_id = running.id
    async with tenant_session(dorg.tenant_id) as db:
        p = await db.get(TalentPool, uuid.UUID(pool["id"]))
        assert p is not None
        again = await pool_refresh.request_refresh(db, p, "manual")
        assert again.id == running_id


async def test_needs_repair_skips_and_notifies(dorg: DOrg) -> None:
    await candidate(dorg.admin, "A", notice_period_days=10)
    pool = await dynamic_pool(dorg.admin)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            update(Segment)
            .where(Segment.id == uuid.UUID(pool["segment_id"]))
            .values(definition={"definition_version": 1, "source": "candidates.candidate", "filters": {"field": "removed_field", "op": "eq", "value": 1}})
        )
    async with tenant_session(dorg.tenant_id) as db:
        p = await db.get(TalentPool, uuid.UUID(pool["id"]))
        assert p is not None
        await pool_refresh.request_refresh(db, p, "nightly")
    last = (await refreshes(dorg.tenant_id, pool["id"]))[-1]
    assert last.status == "skipped_needs_repair"
    async with tenant_session(dorg.tenant_id) as db:
        seg = await db.get(Segment, uuid.UUID(pool["segment_id"]))
        assert seg is not None and seg.needs_repair and seg.repair_issues[0]["code"] == "semantic_unknown_field"
        notes = (await db.execute(select(Notification).where(Notification.type == "pool_needs_repair"))).scalars().all()
    assert [n.user_id for n in notes] == [dorg.admin.user_id]
    assert (await dorg.admin.client.get(f"{POOLS}/{pool['id']}")).json()["needs_repair"] is True


async def test_timeout_fails_refresh_and_notifies(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = await dynamic_pool(dorg.admin)

    async def boom(*_: Any, **__: Any) -> Any:
        raise SemanticTimeout()

    monkeypatch.setattr(pool_refresh.segments, "evaluate", boom)
    async with tenant_session(dorg.tenant_id) as db:
        p = await db.get(TalentPool, uuid.UUID(pool["id"]))
        assert p is not None
        await pool_refresh.request_refresh(db, p, "nightly")
    last = (await refreshes(dorg.tenant_id, pool["id"]))[-1]
    assert (last.status, last.error_code) == ("failed", "query_timeout")
    async with tenant_session(dorg.tenant_id) as db:
        notes = (await db.execute(select(Notification).where(Notification.type == "pool_refresh_failed"))).scalars().all()
    assert len(notes) == 1


async def test_viewer_scope_filters_members_of_system_refreshed_pool(app: FastAPI, dorg: DOrg) -> None:
    rec = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        mine = await candidate(rec, "Owned By Rita", notice_period_days=5)
        other = await candidate(dorg.admin, "Owned By Admin", notice_period_days=5)
        pool = await dynamic_pool(dorg.admin, name="Everyone quick", visibility="tenant")
        assert await active_member_ids(dorg.tenant_id, pool["id"]) == {mine, other}  # system context: tenant-wide
        own_ctx = ctx_for(dorg.tenant_id, rec.user_id, candidate_scope=Scope.OWN)
        async with tenant_session(dorg.tenant_id) as db:
            assert await pools_service.member_ids(own_ctx, db, uuid.UUID(pool["id"])) == [mine]
            p = await db.get(TalentPool, uuid.UUID(pool["id"]))
            assert p is not None
            page = await pool_members.list_members(own_ctx, db, p, page=1, page_size=50)
        assert [i.candidate_id for i in page.items] == [mine] and page.total == 1
        # Recruiters see the tenant pool but cannot create tenant pools (pool:manage)
        listed = (await rec.client.get(POOLS)).json()
        assert [p["id"] for p in listed] == [pool["id"]]
        resp = await rec.client.post(POOLS, json={"name": "T", "visibility": "tenant"}, headers=h(rec))
        assert resp.status_code == 403
        private = await create_pool(dorg.admin, name="Admin private")
        assert (await rec.client.get(f"{POOLS}/{private['id']}")).status_code == 404
    finally:
        await rec.client.aclose()


async def test_cross_tenant_pool_is_404(dorg: DOrg, other_dorg: DOrg) -> None:
    pool = await create_pool(dorg.admin, name="Mine")
    assert (await other_dorg.admin.client.get(f"{POOLS}/{pool['id']}")).status_code == 404
    assert (await other_dorg.admin.client.get(f"{POOLS}/{pool['id']}/members")).status_code == 404
    ctx = ctx_for(other_dorg.tenant_id, other_dorg.admin.user_id)
    async with tenant_session(other_dorg.tenant_id) as db:
        with pytest.raises(Exception) as exc:
            await pools_service.member_ids(ctx, db, uuid.UUID(pool["id"]))
    assert getattr(exc.value, "status_code", None) == 404


async def test_skill_graph_change_marks_due_and_due_beat_refreshes(dorg: DOrg) -> None:
    skill_def = {
        "filters": {
            "field": "skills",
            "op": "skill_match",
            "value": {"ref": {"kind": "global", "id": str(uuid.uuid4())}, "expand": True},
        }
    }
    pool = await dynamic_pool(dorg.admin, skill_def, name="Java")
    plain = await dynamic_pool(dorg.admin, name="Plain")
    async with tenant_session(dorg.tenant_id) as db:
        marked = await pool_refresh.mark_due_for_graph_change(db, dorg.tenant_id, "platform", [])
    assert marked == 1
    assert (await pool_row(dorg.tenant_id, pool["id"])).refresh_due_at is not None
    assert (await pool_row(dorg.tenant_id, plain["id"])).refresh_due_at is None
    async with tenant_session(dorg.tenant_id) as db:
        assert await pool_refresh.request_due(db) == 1
    last = (await refreshes(dorg.tenant_id, pool["id"]))[-1]
    assert (last.trigger, last.status) == ("skill_graph", "done")
    assert (await pool_row(dorg.tenant_id, pool["id"])).refresh_due_at is None
