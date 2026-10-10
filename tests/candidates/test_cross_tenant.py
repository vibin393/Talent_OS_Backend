"""Every Epic 3 endpoint answers 404 for ids that belong to another tenant."""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.candidates import CandidateMerge
from app.services.candidates import resolve_skill
from tests.epic3 import CANDIDATES, FakeAI, Org3, create_candidate, h, resume_pdf, upload

LISTS = "/api/v1/candidate-lists"


async def _tenant_a_fixtures(org: Org3) -> dict[str, Any]:
    cand = await create_candidate(org.admin, full_name="Secret Person", emails=[{"email": "s@example.com"}])
    await create_candidate(org.admin, full_name="Secret Person", emails=[{"email": "s@example.com"}])
    resume = (
        await upload(org.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf(), "cv.pdf")
    ).json()
    draft = (await upload(org.admin.client, f"{CANDIDATES}/resume-drafts", resume_pdf(), "d.pdf")).json()
    att = (
        await upload(
            org.admin.client,
            f"{CANDIDATES}/{cand['id']}/attachments",
            resume_pdf(),
            "a.pdf",
            category="other",
        )
    ).json()
    tags = (
        await org.admin.client.post(
            f"{CANDIDATES}/{cand['id']}/tags", json={"names": ["Secret"]}, headers=h(org.admin)
        )
    ).json()
    lst = (await org.admin.client.post(LISTS, json={"name": "Secret list"}, headers=h(org.admin))).json()
    dup = (await org.admin.client.get(f"{CANDIDATES}/duplicates")).json()["items"][0]
    imp = (
        await org.admin.client.post(
            f"{CANDIDATES}/imports",
            files=[("files", ("x.pdf", resume_pdf(), "application/pdf"))],
            headers=h(org.admin),
        )
    ).json()
    ctx = RequestContext(tenant_id=org.tenant_id)
    async with tenant_session(org.tenant_id) as db:
        pending = await resolve_skill(ctx, db, "SecretLang")
    assert pending is not None
    ai_actions = (await org.admin.client.get(f"{CANDIDATES}/{cand['id']}/ai-actions")).json()
    return {
        "cand": cand["id"],
        "resume": resume["resume"]["id"],
        "resume_file": resume["resume"]["file"]["id"],
        "draft": draft["resume"]["id"],
        "att": att["id"],
        "att_file": att["file"]["id"],
        "tag": tags[0]["id"],
        "list": lst["id"],
        "dup": dup["id"],
        "import": imp["id"],
        "skill": str(pending.id),
        "ai_actions": len(ai_actions),
    }


async def test_every_endpoint_hides_other_tenants_ids(org3: Org3, other3: Org3, fake_ai: FakeAI) -> None:
    a = await _tenant_a_fixtures(org3)
    merged = await org3.admin.client.post(
        f"{CANDIDATES}/duplicates/{a['dup']}/merge", json={"survivor_id": a["cand"]}, headers=h(org3.admin)
    )
    assert merged.status_code == 200, merged.text
    async with tenant_session(org3.tenant_id) as db:
        merge_id = (await db.execute(select(CandidateMerge.id))).scalar_one()
    b = other3.admin
    hb = h(b)
    c = a["cand"]
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", f"{CANDIDATES}/{c}", {}),
        ("PATCH", f"{CANDIDATES}/{c}", {"json": {"current_title": "x"}}),
        ("PUT", f"{CANDIDATES}/{c}/skills", {"json": {"skills": ["Java"]}}),
        ("POST", f"{CANDIDATES}/{c}/archive", {}),
        ("POST", f"{CANDIDATES}/{c}/unarchive", {}),
        ("GET", f"{CANDIDATES}/{c}/resumes", {}),
        (
            "POST",
            f"{CANDIDATES}/{c}/resumes",
            {"files": {"file": ("cv.pdf", resume_pdf(), "application/pdf")}},
        ),
        ("POST", f"{CANDIDATES}/{c}/resumes/{a['resume']}/apply", {"json": {}}),
        ("GET", f"{CANDIDATES}/{c}/ai-actions", {}),
        ("POST", f"{CANDIDATES}/{c}/tags", {"json": {"names": ["x"]}}),
        ("DELETE", f"{CANDIDATES}/{c}/tags/{a['tag']}", {}),
        ("POST", f"{CANDIDATES}/bulk/tags", {"json": {"candidate_ids": [c], "names": ["x"]}}),
        ("GET", f"{CANDIDATES}/duplicates/{a['dup']}", {}),
        ("POST", f"{CANDIDATES}/duplicates/{a['dup']}/merge", {"json": {"survivor_id": c}}),
        ("POST", f"{CANDIDATES}/duplicates/{a['dup']}/keep-separate", {}),
        ("POST", f"{CANDIDATES}/merges/{merge_id}/undo", {}),
        ("GET", f"{CANDIDATES}/imports/{a['import']}", {}),
        ("GET", f"{LISTS}/{a['list']}", {}),
        ("PATCH", f"{LISTS}/{a['list']}", {"json": {"name": "x"}}),
        ("DELETE", f"{LISTS}/{a['list']}", {}),
        ("PUT", f"{LISTS}/{a['list']}/shares", {"json": {}}),
        ("POST", f"{LISTS}/{a['list']}/members", {"json": {"candidate_ids": [c]}}),
        ("DELETE", f"{LISTS}/{a['list']}/members/{c}", {}),
        ("GET", f"/api/v1/files/{a['resume_file']}/download", {}),
        ("GET", f"/api/v1/files/{a['att_file']}/download", {}),
        ("GET", f"{CANDIDATES}/{c}/attachments", {}),
        (
            "POST",
            f"{CANDIDATES}/{c}/attachments",
            {"files": {"file": ("a.pdf", resume_pdf(), "application/pdf")}, "data": {"category": "other"}},
        ),
        ("DELETE", f"/api/v1/attachments/{a['att']}", {}),
        ("POST", f"/api/v1/skills/{a['skill']}/approve", {}),
        ("POST", f"/api/v1/skills/{a['skill']}/reject", {}),
        ("POST", f"/api/v1/skills/tenant/{a['skill']}/aliases", {"json": {"alias": "zz"}}),
    ]
    for method, url, kwargs in calls:
        resp = await b.client.request(method, url, headers=hb, **kwargs)
        assert resp.status_code == 404, f"{method} {url} -> {resp.status_code} {resp.text}"

    # Ids from tenant A in bodies / filters never resolve in tenant B either.
    create = await b.client.post(CANDIDATES, json={"full_name": "B", "resume_id": a["draft"]}, headers=hb)
    assert create.status_code == 404
    assert (await b.client.get(CANDIDATES, params={"list_id": a["list"]})).json()["total"] == 0
    assert (await b.client.get(CANDIDATES, params={"tag_id": a["tag"]})).json()["total"] == 0
    assert (await b.client.get(CANDIDATES)).json()["total"] == 0
    assert (await b.client.get(f"{CANDIDATES}/duplicates")).json()["total"] == 0
    assert (await b.client.get(f"{CANDIDATES}/merges")).json()["total"] == 0
    assert (await b.client.get(f"{CANDIDATES}/imports")).json()["total"] == 0
    assert (await b.client.get(LISTS)).json()["total"] == 0
    assert (await b.client.get("/api/v1/tags")).json()["total"] == 0
    assert (await b.client.get("/api/v1/skills/pending")).json()["total"] == 0
    assert a["ai_actions"] >= 1
    assert (await b.client.get(f"{CANDIDATES}/{uuid.uuid4()}")).status_code == 404
