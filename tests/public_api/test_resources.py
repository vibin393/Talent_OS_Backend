# ruff: noqa: E501
"""Epic 67 (story 67.5): every read resource -- list 200 with the page shape, unknown / cross-tenant id 404 (the same
body as "does not exist"), missing scope 403 ``insufficient_scope``; engagement writes (notes, activities, tasks with
If-Match), demands (draft only) and postings."""

import uuid

import pytest
from fastapi import FastAPI

from tests.epic3 import Org3, create_candidate
from tests.public_api.e67 import Api, app_token, code, installed, user_token

READS = [
    ("/candidates", "candidates:read"), ("/applications", "applications:read"), ("/demands", "demands:read"),
    ("/postings", "postings:read"), ("/clients", "clients:read"), ("/contacts", "contacts:read"), ("/users", "users:read"),
    ("/pools", "pools:read"), ("/submissions", "submissions:read"), ("/interviews", "interviews:read"),
    ("/offers", "offers:read"), ("/placements", "placements:read"), ("/workers", "workers:read"),
    ("/timesheets", "timesheets:read"), ("/expense_claims", "expenses:read"), ("/invoices", "invoices:read"),
    ("/credit_notes", "invoices:read"), ("/payments", "invoices:read"), ("/tasks", "tasks:read"),
]  # fmt: skip


async def test_every_read_resource(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    api = Api(app, await app_token(inst))
    delegated = Api(app, await user_token(inst, org3.admin.user_id))
    for path, _scope in READS:
        for client in (api, delegated):
            resp = await client.get(path, limit=5)
            assert resp.status_code == 200, (path, resp.text)
            body = resp.json()
            assert set(body) == {"data", "next_cursor", "has_more"}, path
        missing = await api.get(f"{path}/{uuid.uuid4()}")
        assert (missing.status_code, code(missing)) == (404, "not_found"), path
    users = (await api.get("/users")).json()["data"]
    assert users and users[0]["email"] is None and "email" in users[0]["masked_fields"]
    assert (await delegated.get("/users")).json()["data"][0]["email"] is not None
    skills = await api.get("/skills", q="py")
    assert skills.status_code == 200


@pytest.mark.parametrize(("path", "scope"), READS)
async def test_missing_scope(app: FastAPI, org3: Org3, path: str, scope: str) -> None:
    inst = await installed(org3, scopes=("skills:read",))
    api = Api(app, await app_token(inst, scopes=("skills:read",)))
    resp = await api.get(path)
    assert (resp.status_code, code(resp)) == (403, "insufficient_scope")
    assert scope in resp.json()["error"]["details"]["required_scopes"]


async def test_notes_activities_tasks(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    cand = await create_candidate(org3.admin, full_name="Noted Person")
    api = Api(app, await user_token(inst, org3.admin.user_id))
    note = await api.post("/notes", {"entity_type": "candidate", "entity_id": cand["id"], "body": "Spoke today"})
    assert note.status_code == 201, note.text
    assert note.json()["author_id"] == str(org3.admin.user_id)
    act = await api.post("/activities", {"kind": "call", "entity_type": "candidate", "entity_id": cand["id"],
                                         "occurred_at": "2026-10-09T10:00:00Z", "summary": "Intro call"})  # fmt: skip
    assert act.status_code == 201, act.text
    notes = (await api.get("/notes", entity_type="candidate", entity_id=cand["id"])).json()["data"]
    acts = (await api.get("/activities", entity_type="candidate", entity_id=cand["id"])).json()["data"]
    assert len(notes) == 1 and len(acts) == 2
    assert (await api.get("/notes", entity_type="candidate", entity_id=str(uuid.uuid4()))).status_code == 404
    # app-only token: notes are authorless; tasks need an assignee
    app_api = Api(app, await app_token(inst))
    anon = await app_api.post("/notes", {"entity_type": "candidate", "entity_id": cand["id"], "body": "From the app"})
    assert anon.status_code == 201 and anon.json()["author_id"] is None
    no_owner = await app_api.post("/tasks", {"title": "Follow up"})
    assert no_owner.status_code == 422
    task = await app_api.post("/tasks", {"title": "Follow up", "assignee_id": str(org3.admin.user_id), "entity_type": "candidate", "entity_id": cand["id"]})
    assert task.status_code == 201, task.text
    t = task.json()
    assert code(await api.patch(f"/tasks/{t['id']}", {"status": "done"})) == "precondition_required"
    done = await api.patch(f"/tasks/{t['id']}", {"status": "done"}, if_match=f'W/"{t["version"]}"')
    assert done.status_code == 200 and done.json()["status"] == "done", done.text
    stale = await api.patch(f"/tasks/{t['id']}", {"title": "x"}, if_match=f'W/"{t["version"]}"')
    assert (stale.status_code, code(stale)) == (412, "precondition_failed")


async def test_demand_draft_create(app: FastAPI, org3: Org3) -> None:
    from tests.modules.demands.helpers import create_client

    inst = await installed(org3)
    client_id = await create_client(org3.admin)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    resp = await api.post("/demands", {"title": "Java developer", "client_id": str(client_id)})
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft" and resp.json()["owner_id"] == str(org3.admin.user_id)
    no_key = await api.post("/demands", {"title": "x", "client_id": str(client_id)}, key=None)
    assert (no_key.status_code, code(no_key)) == (422, "idempotency_key_required")
    extra = await api.post("/demands", {"title": "x", "client_id": str(client_id), "status": "open"})
    assert extra.status_code == 422
