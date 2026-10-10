"""Story 3.5: merge with field winners, children moved, audit, undo (window / once)."""

from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.candidates import CandidateMerge
from app.services.candidates.common import now
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, member, resume_pdf, upload

DUPS = f"{CANDIDATES}/duplicates"


async def _pair(org: Org3) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    a = await create_candidate(
        org.admin,
        full_name="Rahul Sharma",
        current_title="Developer",
        emails=[{"email": "rahul@example.com"}],
        skills=[{"name": "Java", "years": 3}],
        tags=["Java"],
    )
    b = await create_candidate(
        org.admin,
        full_name="Rahul K Sharma",
        current_title="Senior Developer",
        emails=[{"email": "rahul@example.com"}, {"email": "rahul.k@example.com"}],
        phones=[{"phone": "9876543210"}],
        skills=[{"name": "Java", "years": 5}, "Python"],
        tags=["Remote"],
        employment=[{"employer": "Acme", "title": "Dev"}],
    )
    await upload(org.admin.client, f"{CANDIDATES}/{a['id']}/resumes", resume_pdf(), "a.pdf")
    await upload(org.admin.client, f"{CANDIDATES}/{b['id']}/resumes", resume_pdf(), "b.pdf")
    await upload(
        org.admin.client, f"{CANDIDATES}/{b['id']}/attachments", resume_pdf(), "c.pdf", category="other"
    )
    lst = await org.admin.client.post(
        "/api/v1/candidate-lists", json={"name": "Shortlist"}, headers=h(org.admin)
    )
    await org.admin.client.post(
        f"/api/v1/candidate-lists/{lst.json()['id']}/members",
        json={"candidate_ids": [b["id"]]},
        headers=h(org.admin),
    )
    item = (await org.admin.client.get(DUPS)).json()["items"][0]
    return a, b, item


async def test_merge_moves_everything_and_undo_restores(org3: Org3) -> None:
    a, b, item = await _pair(org3)
    side_b = "a" if item["candidate_a"]["id"] == b["id"] else "b"
    resp = await org3.admin.client.post(
        f"{DUPS}/{item['id']}/merge",
        json={"survivor_id": a["id"], "field_choices": {"current_title": side_b}},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    merge = resp.json()
    assert merge["can_undo"] is True

    survivor = (await org3.admin.client.get(f"{CANDIDATES}/{a['id']}")).json()
    loser = (await org3.admin.client.get(f"{CANDIDATES}/{b['id']}")).json()
    assert loser["merged_into_id"] == a["id"]  # 200 with merged_into_id so the UI can redirect
    assert loser["archived_at"] is not None
    assert survivor["current_title"] == "Senior Developer"
    assert survivor["full_name"] == "Rahul Sharma"
    assert sorted(e["email"] for e in survivor["emails"]) == ["rahul.k@example.com", "rahul@example.com"]
    assert [p["phone_e164"] for p in survivor["phones"]] == ["+919876543210"]
    skills = {s["skill"]["name"]: s["years"] for s in survivor["skills"]}
    assert skills == {"Java": "5.0", "Python": None}
    assert sorted(t["name"] for t in survivor["tags"]) == ["Java", "Remote"]
    assert [r["version"] for r in survivor["resumes"]] == [2, 1]
    assert survivor["counts"]["attachments"] == 1
    assert [lst["name"] for lst in survivor["lists"]] == ["Shortlist"]
    assert len(survivor["employment"]) == 1
    listed = (await org3.admin.client.get(CANDIDATES)).json()
    assert [c["id"] for c in listed["items"]] == [a["id"]]  # merged candidates hidden
    assert (await org3.admin.client.get(DUPS, params={"status": "merged"})).json()["total"] == 1

    merges = (await org3.admin.client.get(f"{CANDIDATES}/merges")).json()
    assert [m["id"] for m in merges["items"]] == [merge["id"]]

    undo = await org3.admin.client.post(f"{CANDIDATES}/merges/{merge['id']}/undo", headers=h(org3.admin))
    assert undo.status_code == 200, undo.text
    assert undo.json()["undone_at"] is not None
    again = await org3.admin.client.post(f"{CANDIDATES}/merges/{merge['id']}/undo", headers=h(org3.admin))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "merge_already_undone"

    a2 = (await org3.admin.client.get(f"{CANDIDATES}/{a['id']}")).json()
    b2 = (await org3.admin.client.get(f"{CANDIDATES}/{b['id']}")).json()
    assert a2["current_title"] == "Developer"
    assert b2["merged_into_id"] is None
    assert b2["archived_at"] is None
    assert [e["email"] for e in a2["emails"]] == ["rahul@example.com"]
    assert sorted(e["email"] for e in b2["emails"]) == ["rahul.k@example.com", "rahul@example.com"]
    assert {s["skill"]["name"]: s["years"] for s in a2["skills"]} == {"Java": "3.0"}
    assert [t["name"] for t in b2["tags"]] == ["Remote"]
    assert [r["version"] for r in a2["resumes"]] == [1]
    assert [r["version"] for r in b2["resumes"]] == [1]
    assert b2["counts"]["attachments"] == 1
    assert [lst["name"] for lst in b2["lists"]] == ["Shortlist"]
    assert a2["lists"] == []

    async with tenant_session(org3.tenant_id) as db:
        actions = set(
            (
                await db.execute(select(AuditLog.action).where(AuditLog.action.like("candidate.merge%")))
            ).scalars()
        )
    assert actions == {"candidate.merged", "candidate.merge_undone"}


async def test_undo_expired(org3: Org3) -> None:
    a, _, item = await _pair(org3)
    merge = (
        await org3.admin.client.post(
            f"{DUPS}/{item['id']}/merge", json={"survivor_id": a["id"]}, headers=h(org3.admin)
        )
    ).json()
    async with tenant_session(org3.tenant_id) as db:
        row = await db.get(CandidateMerge, merge["id"])
        assert row is not None
        row.undo_until = now() - timedelta(seconds=1)
    resp = await org3.admin.client.post(f"{CANDIDATES}/merges/{merge['id']}/undo", headers=h(org3.admin))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "merge_undo_expired"


async def test_merge_requires_permission_and_valid_survivor(app: FastAPI, org3: Org3) -> None:
    a, _, item = await _pair(org3)
    recruiter = await member(app, org3, "recruiter")
    try:
        denied = await recruiter.client.post(
            f"{DUPS}/{item['id']}/merge", json={"survivor_id": a["id"]}, headers=h(recruiter)
        )
        assert denied.status_code == 403
    finally:
        await recruiter.client.aclose()
    other = await create_candidate(org3.admin, full_name="Unrelated Person")
    bad = await org3.admin.client.post(
        f"{DUPS}/{item['id']}/merge", json={"survivor_id": other["id"]}, headers=h(org3.admin)
    )
    assert bad.status_code == 422
    bad_field = await org3.admin.client.post(
        f"{DUPS}/{item['id']}/merge",
        json={"survivor_id": a["id"], "field_choices": {"human_id": "a"}},
        headers=h(org3.admin),
    )
    assert bad_field.status_code == 422
    ok = await org3.admin.client.post(
        f"{DUPS}/{item['id']}/merge", json={"survivor_id": a["id"]}, headers=h(org3.admin)
    )
    assert ok.status_code == 200
    twice = await org3.admin.client.post(
        f"{DUPS}/{item['id']}/merge", json={"survivor_id": a["id"]}, headers=h(org3.admin)
    )
    assert twice.status_code == 409


async def test_notes_and_timeline_follow_the_merge(org3: Org3) -> None:
    a, b, item = await _pair(org3)
    created = (await org3.admin.client.get(f"{CANDIDATES}/{a['id']}/timeline")).json()
    assert "Candidate created" in [e["body"] for e in created["items"]]
    note = await org3.admin.client.post(
        f"{CANDIDATES}/{b['id']}/activities", json={"body": "Spoke to candidate"}, headers=h(org3.admin)
    )
    assert note.status_code == 201, note.text
    merge = (
        await org3.admin.client.post(
            f"{DUPS}/{item['id']}/merge", json={"survivor_id": a["id"]}, headers=h(org3.admin)
        )
    ).json()
    survivor_bodies = [
        e["body"] for e in (await org3.admin.client.get(f"{CANDIDATES}/{a['id']}/timeline")).json()["items"]
    ]
    assert "Spoke to candidate" in survivor_bodies
    assert "Candidate merged" in survivor_bodies
    undo = await org3.admin.client.post(f"{CANDIDATES}/merges/{merge['id']}/undo", headers=h(org3.admin))
    assert undo.status_code == 200
    loser_bodies = [
        e["body"] for e in (await org3.admin.client.get(f"{CANDIDATES}/{b['id']}/timeline")).json()["items"]
    ]
    assert "Spoke to candidate" in loser_bodies
