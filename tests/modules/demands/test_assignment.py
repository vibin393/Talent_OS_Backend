"""Story 4.5: recruiter assignment, team visibility, first-action hooks."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.demands import DemandAssigned
from app.services import demands as svc
from tests.modules.demands.helpers import DEMANDS, DOrg, admin_ctx, create_demand, h, open_demand
from tests.modules.identity.conftest import join


async def _team(org: DOrg, *user_ids: uuid.UUID) -> uuid.UUID:
    m = org.admin
    resp = await m.client.post(
        "/api/v1/admin/teams", json={"name": f"Team {uuid.uuid4().hex[:6]}"}, headers=h(m)
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]
    for uid in user_ids:
        r = await m.client.put(f"/api/v1/admin/teams/{team_id}/members/{uid}", json={}, headers=h(m))
        assert r.status_code in (200, 201, 204), r.text
    return uuid.UUID(team_id)


async def test_assign_users_and_team(app: FastAPI, dorg: DOrg) -> None:
    u1 = await join(app, dorg.tenant, "recruiter", name="Uma One")
    u2 = await join(app, dorg.tenant, "recruiter", name="Uday Two")
    t1 = await join(app, dorg.tenant, "recruiter", name="Tara Team")
    events: list[DemandAssigned] = []
    subscribe(DemandAssigned, events.append)
    try:
        team_id = await _team(dorg, t1.user_id)
        opened = await open_demand(dorg.admin, dorg.client_id)
        url = f"{DEMANDS}/{opened['id']}/assignments"
        m = dorg.admin
        bad = await m.client.put(
            url, json={"user_ids": [str(u1.user_id)], "primary_user_id": str(u2.user_id)}, headers=h(m)
        )
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, "primary_not_assigned")
        bad = await m.client.put(url, json={"user_ids": [str(uuid.uuid4())]}, headers=h(m))
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_reference")

        body = {
            "user_ids": [str(u1.user_id), str(u2.user_id)],
            "team_ids": [str(team_id)],
            "primary_user_id": str(u1.user_id),
        }
        resp = await m.client.put(url, json=body, headers=h(m))
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 3
        assert items[0]["user_id"] == str(u1.user_id) and items[0]["is_primary"] is True
        assert events and events[-1].primary_user_id == u1.user_id
        assert set(events[-1].added_user_ids) == {u1.user_id, u2.user_id}
        assert events[-1].expanded_team_member_ids == (t1.user_id,)

        # Team member sees it under "Assigned to me".
        mine = (await t1.client.get(DEMANDS, params={"assigned_to": "me"})).json()["items"]
        assert [i["id"] for i in mine] == [opened["id"]]
        by_user = (await m.client.get(DEMANDS, params={"assigned_to": str(t1.user_id)})).json()["items"]
        assert [i["id"] for i in by_user] == [opened["id"]]

        # Replace: drop u2 and the team, make u2... primary moves back to nobody.
        resp = await m.client.put(
            url, json={"user_ids": [str(u1.user_id)], "primary_user_id": None}, headers=h(m)
        )
        items = resp.json()["items"]
        assert [(i["user_id"], i["is_primary"]) for i in items] == [(str(u1.user_id), False)]
        resp = await m.client.put(
            url,
            json={"user_ids": [str(u1.user_id), str(u2.user_id)], "primary_user_id": str(u2.user_id)},
            headers=h(m),
        )
        assert [i["user_id"] for i in resp.json()["items"] if i["is_primary"]] == [str(u2.user_id)]
        assert (await t1.client.get(DEMANDS, params={"assigned_to": "me"})).json()["items"] == []

        draft = await create_demand(m, dorg.client_id)
        resp = await m.client.put(
            f"{DEMANDS}/{draft['id']}/assignments", json={"user_ids": [str(u1.user_id)]}, headers=h(m)
        )
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "demand_not_assignable")

        # Recruiters lack demand:assign.
        resp = await u1.client.put(url, json={"user_ids": []}, headers=h(u1))
        assert resp.status_code == 403
    finally:
        unsubscribe(DemandAssigned, events.append)
        for member in (u1, u2, t1):
            await member.client.aclose()


async def test_first_action_hooks(app: FastAPI, dorg: DOrg) -> None:
    t1 = await join(app, dorg.tenant, "recruiter", name="Tara Team")
    try:
        team_id = await _team(dorg, t1.user_id)
        opened = await open_demand(dorg.admin, dorg.client_id)
        demand_id = uuid.UUID(opened["id"])
        await dorg.admin.client.put(
            f"{DEMANDS}/{demand_id}/assignments", json={"team_ids": [str(team_id)]}, headers=h(dorg.admin)
        )
        first = datetime(2026, 10, 5, 10, tzinfo=UTC)
        ctx = admin_ctx(dorg)
        async with tenant_session(dorg.tenant_id) as db:
            assert await svc.record_first_activity(ctx, db, demand_id, t1.user_id, first) is True
            assert (
                await svc.record_first_activity(ctx, db, demand_id, t1.user_id, first + timedelta(hours=1))
                is False
            )
            assert await svc.record_first_submission(ctx, db, demand_id, t1.user_id, first) is True
            assert await svc.record_first_application(ctx, db, demand_id, uuid.uuid4(), first) is False
        items = (await dorg.admin.client.get(f"{DEMANDS}/{demand_id}/assignments")).json()["items"]
        assert items[0]["first_activity_at"].startswith("2026-10-05T10:00")
        assert items[0]["first_submission_at"] is not None
        assert items[0]["first_application_at"] is None
    finally:
        await t1.client.aclose()
