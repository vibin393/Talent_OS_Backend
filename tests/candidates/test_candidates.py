"""Story 3.1: candidate records, CAN ids, normalisation, compensation masking, owner change, scoping."""

import uuid

from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, member


async def test_create_assigns_sequential_human_ids_and_normalises(org3: Org3) -> None:
    first = await create_candidate(
        org3.admin,
        full_name="Asha Iyer",
        emails=[{"email": "Asha.Iyer@Example.COM"}],
        phones=[{"phone": "9876543210"}],
        links=[{"url": "https://www.LinkedIn.com/in/asha/?utm=1"}],
        location={"city": "Pune", "country": "India"},
        current_ctc_amount="1800000.00",
        current_ctc_currency="inr",
        current_ctc_unit="annual",
    )
    second = await create_candidate(org3.admin, full_name="Babu K")
    assert first["human_id"] == "CAN-000001"
    assert second["human_id"] == "CAN-000002"
    assert first["emails"][0]["email"] == "asha.iyer@example.com"
    assert first["emails"][0]["is_primary"] is True
    assert first["phones"][0]["phone_e164"] == "+919876543210"
    assert first["links"][0]["url_norm"] == "linkedin.com/in/asha"
    assert first["links"][0]["kind"] == "linkedin"
    assert first["source"] == "manual"
    assert first["owner_id"] == str(org3.admin.user_id)
    assert first["current_ctc_amount"] == "1800000.00"  # admin has compensation:view
    assert first["current_ctc_currency"] == "INR"
    assert first["applications"] == []
    assert first["warnings"]["possible_duplicates"] == []


async def test_invalid_phone_is_422(org3: Org3) -> None:
    resp = await org3.admin.client.post(
        CANDIDATES, json={"full_name": "X Y", "phones": [{"phone": "12345"}]}, headers=h(org3.admin)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_phone"


async def test_compensation_is_masked_without_compensation_view(app: FastAPI, org3: Org3) -> None:
    created = await create_candidate(
        org3.admin, current_ctc_amount="100.00", current_ctc_currency="INR", expected_ctc_amount="200.00"
    )
    recruiter = await member(app, org3, "recruiter")
    try:
        resp = await recruiter.client.get(f"{CANDIDATES}/{created['id']}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("current_ctc_amount", "current_ctc_currency", "expected_ctc_amount"):
            assert body[key] is None
        assert "current_ctc_amount" in body["masked_fields"]
        assert body["full_name"] == "Rahul Sharma"
    finally:
        await recruiter.client.aclose()


async def test_owner_change_is_audited(app: FastAPI, org3: Org3) -> None:
    created = await create_candidate(org3.admin)
    other = await member(app, org3, "recruiter", name="Omar Owner")
    try:
        resp = await org3.admin.client.patch(
            f"{CANDIDATES}/{created['id']}", json={"owner_id": str(other.user_id)}, headers=h(org3.admin)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] == str(other.user_id)
        assert resp.json()["owner_name"] == "Omar Owner"
    finally:
        await other.client.aclose()
    async with tenant_session(org3.tenant_id) as db:
        actions = set(
            (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == created["id"]))).scalars()
        )
    assert {"candidate.created", "candidate.updated", "candidate.owner_changed"} <= actions


async def test_unknown_owner_is_422(org3: Org3, other3: Org3) -> None:
    resp = await org3.admin.client.post(
        CANDIDATES, json={"full_name": "A B", "owner_id": str(other3.admin.user_id)}, headers=h(org3.admin)
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_reference"


async def test_patch_replaces_child_lists(org3: Org3) -> None:
    created = await create_candidate(
        org3.admin,
        emails=[{"email": "a@example.com"}, {"email": "b@example.com"}],
        employment=[{"employer": "Acme", "title": "Dev"}],
    )
    resp = await org3.admin.client.patch(
        f"{CANDIDATES}/{created['id']}",
        json={"emails": [{"email": "c@example.com"}], "current_title": "Lead"},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["email"] for e in body["emails"]] == ["c@example.com"]
    assert len(body["employment"]) == 1  # not provided -> untouched
    assert body["current_title"] == "Lead"


async def test_list_search_filters_and_archive(org3: Org3) -> None:
    a = await create_candidate(org3.admin, full_name="Zed Alpha", emails=[{"email": "zed@findme.io"}])
    await create_candidate(org3.admin, full_name="Yan Beta", current_employer="Wipro")
    resp = await org3.admin.client.get(CANDIDATES, params={"q": "findme"})
    assert [c["id"] for c in resp.json()["items"]] == [a["id"]]
    resp = await org3.admin.client.get(CANDIDATES, params={"q": "wipro"})
    assert resp.json()["total"] == 1
    resp = await org3.admin.client.get(CANDIDATES, params={"q": "CAN-000001"})
    assert resp.json()["items"][0]["id"] == a["id"]
    resp = await org3.admin.client.get(CANDIDATES, params={"sort": "name"})
    assert [c["full_name"] for c in resp.json()["items"]] == ["Yan Beta", "Zed Alpha"]
    assert resp.json()["page"] == 1

    archived = await org3.admin.client.post(f"{CANDIDATES}/{a['id']}/archive", headers=h(org3.admin))
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert (await org3.admin.client.get(CANDIDATES)).json()["total"] == 1
    assert (await org3.admin.client.get(CANDIDATES, params={"include_archived": True})).json()["total"] == 2
    restored = await org3.admin.client.post(f"{CANDIDATES}/{a['id']}/unarchive", headers=h(org3.admin))
    assert restored.json()["archived_at"] is None


async def test_scope_own_sees_only_owned(app: FastAPI, org3: Org3) -> None:
    # The Interviewer role holds candidate:read at *own* scope.
    interviewer = await member(app, org3, "interviewer", name="Ivy Interviewer")
    try:
        mine = await create_candidate(org3.admin, full_name="Owned By Ivy", owner_id=str(interviewer.user_id))
        theirs = await create_candidate(org3.admin, full_name="Not Owned")
        listed = await interviewer.client.get(CANDIDATES)
        assert [c["id"] for c in listed.json()["items"]] == [mine["id"]]
        assert (await interviewer.client.get(f"{CANDIDATES}/{theirs['id']}")).status_code == 404
        assert (await interviewer.client.get(f"{CANDIDATES}/{mine['id']}")).status_code == 200
    finally:
        await interviewer.client.aclose()


async def test_permissions(app: FastAPI, org3: Org3) -> None:
    created = await create_candidate(org3.admin)
    viewer = await member(app, org3, "viewer", name="Val Viewer")
    try:
        assert (await viewer.client.get(f"{CANDIDATES}/{created['id']}")).status_code == 200
        denied = await viewer.client.post(CANDIDATES, json={"full_name": "Nope"}, headers=h(viewer))
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["permission"] == "candidate:create"
        patch = await viewer.client.patch(
            f"{CANDIDATES}/{created['id']}", json={"full_name": "N"}, headers=h(viewer)
        )
        assert patch.status_code == 403
        archive = await viewer.client.post(f"{CANDIDATES}/{created['id']}/archive", headers=h(viewer))
        assert archive.status_code == 403
    finally:
        await viewer.client.aclose()


async def test_skills_set_by_text_and_ref(org3: Org3) -> None:
    created = await create_candidate(org3.admin, skills=["K8s", {"name": "JS", "years": "4.5"}, "k8s"])
    names = {s["skill"]["name"]: s for s in created["skills"]}
    assert set(names) == {"Kubernetes", "JavaScript"}
    assert names["JavaScript"]["years"] == "4.5"
    ref = names["Kubernetes"]["skill"]
    resp = await org3.admin.client.put(
        f"{CANDIDATES}/{created['id']}/skills",
        json={"skills": [{"ref": {"kind": ref["kind"], "id": ref["id"]}, "years": 2}]},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    assert [s["skill"]["name"] for s in resp.json()] == ["Kubernetes"]
    bad = await org3.admin.client.put(
        f"{CANDIDATES}/{created['id']}/skills",
        json={"skills": [{"ref": {"kind": "global", "id": str(uuid.uuid4())}}]},
        headers=h(org3.admin),
    )
    assert bad.status_code == 422


async def test_candidate_360_shape(org3: Org3) -> None:
    created = await create_candidate(org3.admin, tags=["Java", "java", "Remote"])
    resp = await org3.admin.client.get(f"{CANDIDATES}/{created['id']}")
    body = resp.json()
    for key in (
        "emails",
        "phones",
        "links",
        "employment",
        "education",
        "certifications",
        "skills",
        "tags",
        "lists",
        "resumes",
        "counts",
        "applications",
        "merged_into_id",
    ):
        assert key in body
    assert sorted(t["name"] for t in body["tags"]) == ["Java", "Remote"]
    assert body["counts"]["tags"] == 2
    assert body["can_edit"] is True
