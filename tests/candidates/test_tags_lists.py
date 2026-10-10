"""Story 3.7: tags (case-insensitive, created on use, bulk) and saved lists (sharing, membership)."""

from typing import Any

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.identity import Team, TeamMember
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, member

LISTS = "/api/v1/candidate-lists"


async def test_tags_are_case_insensitive(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    resp = await org3.admin.client.post(
        f"{CANDIDATES}/{cand['id']}/tags", json={"names": ["Java", "java", " JAVA "]}, headers=h(org3.admin)
    )
    assert resp.status_code == 200, resp.text
    assert [t["name"] for t in resp.json()] == ["Java"]
    other = await create_candidate(org3.admin, full_name="Other Person")
    again = await org3.admin.client.post(
        f"{CANDIDATES}/{other['id']}/tags", json={"names": ["JAVA"]}, headers=h(org3.admin)
    )
    assert again.json()[0]["id"] == resp.json()[0]["id"]
    tags = (await org3.admin.client.get("/api/v1/tags", params={"q": "ja"})).json()
    assert tags["total"] == 1
    tag_id = resp.json()[0]["id"]
    filtered = (await org3.admin.client.get(CANDIDATES, params={"tag_id": tag_id})).json()
    assert filtered["total"] == 2
    removed = await org3.admin.client.delete(
        f"{CANDIDATES}/{cand['id']}/tags/{tag_id}", headers=h(org3.admin)
    )
    assert removed.json() == []
    missing = await org3.admin.client.delete(
        f"{CANDIDATES}/{cand['id']}/tags/{tag_id}", headers=h(org3.admin)
    )
    assert missing.status_code == 404


async def test_bulk_tags(org3: Org3) -> None:
    a = await create_candidate(org3.admin, full_name="Bulk A")
    b = await create_candidate(org3.admin, full_name="Bulk B")
    resp = await org3.admin.client.post(
        f"{CANDIDATES}/bulk/tags",
        json={"candidate_ids": [a["id"], b["id"]], "names": ["Hot", "Q4"]},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    body = (await org3.admin.client.get(f"{CANDIDATES}/{b['id']}")).json()
    assert sorted(t["name"] for t in body["tags"]) == ["Hot", "Q4"]


async def _list(m: Any, name: str = "Shortlist") -> dict[str, Any]:
    resp = await m.client.post(LISTS, json={"name": name, "description": "d"}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_list_sharing_with_team_and_membership(app: FastAPI, org3: Org3) -> None:
    teammate = await member(app, org3, "recruiter", name="Tara Teammate")
    outsider = await member(app, org3, "recruiter", name="Oscar Outsider")
    try:
        async with tenant_session(org3.tenant_id) as db:
            team = Team(tenant_id=org3.tenant_id, name="Delivery")
            db.add(team)
            await db.flush()
            db.add(TeamMember(tenant_id=org3.tenant_id, team_id=team.id, user_id=teammate.user_id))
            team_id = team.id
        lst = await _list(org3.admin)
        cand = await create_candidate(org3.admin)
        assert (await teammate.client.get(f"{LISTS}/{lst['id']}")).status_code == 404

        denied = await teammate.client.put(
            f"{LISTS}/{lst['id']}/shares", json={"team_ids": [str(team_id)]}, headers=h(teammate)
        )
        assert denied.status_code == 404  # not visible yet
        shared = await org3.admin.client.put(
            f"{LISTS}/{lst['id']}/shares", json={"team_ids": [str(team_id)]}, headers=h(org3.admin)
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["shared_team_ids"] == [str(team_id)]

        # Team member sees it (access is reloaded every request); outsider does not.
        seen = await teammate.client.get(f"{LISTS}/{lst['id']}")
        assert seen.status_code == 200
        assert seen.json()["is_owner"] is False
        assert (await outsider.client.get(f"{LISTS}/{lst['id']}")).status_code == 404
        assert (await teammate.client.get(LISTS)).json()["total"] == 1
        assert (await outsider.client.get(LISTS)).json()["total"] == 0

        # Shared users manage members but cannot share or rename.
        added = await teammate.client.post(
            f"{LISTS}/{lst['id']}/members", json={"candidate_ids": [cand["id"]]}, headers=h(teammate)
        )
        assert added.status_code == 200, added.text
        assert added.json()["member_count"] == 1
        reshare = await teammate.client.put(f"{LISTS}/{lst['id']}/shares", json={}, headers=h(teammate))
        assert reshare.status_code == 403
        rename = await teammate.client.patch(
            f"{LISTS}/{lst['id']}", json={"name": "Mine"}, headers=h(teammate)
        )
        assert rename.status_code == 403

        listed = (await org3.admin.client.get(CANDIDATES, params={"list_id": lst["id"]})).json()
        assert [c["id"] for c in listed["items"]] == [cand["id"]]
        removed = await teammate.client.delete(
            f"{LISTS}/{lst['id']}/members/{cand['id']}", headers=h(teammate)
        )
        assert removed.json()["member_count"] == 0
        still = await org3.admin.client.get(f"{CANDIDATES}/{cand['id']}")
        assert still.status_code == 200  # removing from a list never touches the candidate
        assert still.json()["archived_at"] is None
    finally:
        await teammate.client.aclose()
        await outsider.client.aclose()


async def test_list_share_with_user_and_owner_crud(app: FastAPI, org3: Org3, other3: Org3) -> None:
    colleague = await member(app, org3, "recruiter", name="Cody Colleague")
    try:
        lst = await _list(org3.admin, "Java devs")
        resp = await org3.admin.client.put(
            f"{LISTS}/{lst['id']}/shares", json={"user_ids": [str(colleague.user_id)]}, headers=h(org3.admin)
        )
        assert resp.json()["shared_user_ids"] == [str(colleague.user_id)]
        assert (await colleague.client.get(f"{LISTS}/{lst['id']}")).status_code == 200
        foreign = await org3.admin.client.put(
            f"{LISTS}/{lst['id']}/shares",
            json={"user_ids": [str(other3.admin.user_id)]},
            headers=h(org3.admin),
        )
        assert foreign.status_code == 422
        renamed = await org3.admin.client.patch(
            f"{LISTS}/{lst['id']}", json={"name": "Java"}, headers=h(org3.admin)
        )
        assert renamed.json()["name"] == "Java"
        archived = await org3.admin.client.delete(f"{LISTS}/{lst['id']}", headers=h(org3.admin))
        assert archived.json()["archived_at"] is not None
        assert (await org3.admin.client.get(LISTS)).json()["total"] == 0
        assert (await org3.admin.client.get(LISTS, params={"include_archived": True})).json()["total"] == 1
    finally:
        await colleague.client.aclose()
