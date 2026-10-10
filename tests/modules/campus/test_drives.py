# ruff: noqa: E501
"""Epic 43 story 43.1: institutions, placement officers, drives, links + QR + poster, the drive state machine."""

import uuid

from fastapi import FastAPI

from app.core.permissions import DEFAULT_ROLES
from tests.modules.campus.helpers import CAMPUS, PUBLIC, create_drive, institution, ready_drive, scalar, token
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import new_client


async def test_campus_recruiter_role_declared() -> None:
    role = next(r for r in DEFAULT_ROLES if r.key == "campus_recruiter")
    assert role.name == "Campus Recruiter"
    for key in ("campus:read", "campus:manage", "event:manage", "campus_eligibility:override", "offer:create"):
        assert key in role.permissions
    for key in ("compensation:view", "offer:approve", "offer:release"):
        assert key not in role.permissions
    rm = next(r for r in DEFAULT_ROLES if r.key == "recruitment_manager")
    assert {"campus:read", "campus:manage", "event:manage", "campus_eligibility:override"} <= set(rm.permissions)


async def test_institution_and_placement_officers(dorg: DOrg) -> None:
    inst = await institution(dorg, "Anna University")
    dup = await dorg.admin.client.post(f"{CAMPUS}/institutions", json={"name": "anna university"}, headers=h(dorg.admin))
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "institution_exists"
    url = f"{CAMPUS}/institutions/{inst['id']}/contacts"
    r = await dorg.admin.client.post(url, json={"name": "Meera TPO", "email": "tpo@anna.example.edu", "phone": "98400 12345", "is_primary": True}, headers=h(dorg.admin))
    assert r.status_code == 201, r.text
    assert r.json()["phone"] == "+919840012345"
    bad = await dorg.admin.client.post(url, json={"name": "X", "phone": "12"}, headers=h(dorg.admin))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_phone"
    again = await dorg.admin.client.post(url, json={"name": "Other", "email": "TPO@anna.example.edu"}, headers=h(dorg.admin))
    assert again.status_code == 409
    second = await dorg.admin.client.post(url, json={"name": "Second", "is_primary": True}, headers=h(dorg.admin))
    detail = (await dorg.admin.client.get(f"{CAMPUS}/institutions/{inst['id']}")).json()
    assert [c["name"] for c in detail["contacts"] if c["is_primary"]] == ["Second"]
    arch = await dorg.admin.client.delete(f"{url}/{second.json()['id']}", headers=h(dorg.admin))
    assert arch.json()["archived_at"] is not None
    listed = (await dorg.admin.client.get(f"{CAMPUS}/institutions", params={"q": "anna"})).json()
    assert listed[0]["contact_count"] == 1


async def test_drive_with_twelve_institutions_gets_twelve_links_and_qrs(dorg: DOrg) -> None:
    drive = await ready_drive(dorg, institutions=12)
    assert drive["status"] == "registration_open"
    urls = [i["registration_url"] for i in drive["institutions"]]
    assert len(urls) == 12 and len(set(urls)) == 12 and all(u and "/campus/r/" in u for u in urls)
    assert all(i["has_qr"] and i["link_active"] for i in drive["institutions"])
    assert len(drive["demands"]) == 1
    files = await scalar(dorg, "SELECT count(*) FROM stored_files WHERE owner_entity_type = 'drive_institution'")
    assert files == 12
    di = drive["institutions"][0]
    for fmt, ctype in (("png", "image/png"), ("svg", "image/svg+xml"), ("pdf", "application/pdf")):
        r = await dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}/institutions/{di['id']}/qr", params={"format": fmt})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith(ctype)
    assert (await dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}/institutions/{di['id']}/qr", params={"format": "pdf"})).content.startswith(b"%PDF")


async def test_open_registration_requires_institution_and_demand(dorg: DOrg) -> None:
    drive = await create_drive(dorg)
    r = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/open-registration", headers=h(dorg.admin))
    assert r.status_code == 409 and r.json()["error"]["code"] == "drive_incomplete"
    assert set(r.json()["error"]["details"]["fields"]) == {"institutions", "demands"}
    close = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/close", headers=h(dorg.admin))
    assert close.status_code == 409 and close.json()["error"]["code"] == "transition_not_allowed"


async def test_cancel_revokes_links(app: FastAPI, dorg: DOrg) -> None:
    drive = await ready_drive(dorg, institutions=2)
    tok = token(drive["institutions"][0]["registration_url"])
    anon = new_client(app)
    assert (await anon.get(f"{PUBLIC}/r/{tok}")).status_code == 200
    r = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/cancel", json={"reason": "Campus closed for exams"}, headers=h(dorg.admin))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    gone = await anon.get(f"{PUBLIC}/r/{tok}")
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "link_revoked"
    again = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/cancel", json={"reason": "again please"}, headers=h(dorg.admin))
    assert again.status_code == 409
    await anon.aclose()


async def test_form_refuses_protected_fields(dorg: DOrg) -> None:
    for key, label in (("caste", "Caste"), ("community_info", "Religion"), ("dob", "Date of birth"), ("gender", "Gender")):
        r = await dorg.admin.client.post(
            f"{CAMPUS}/drives",
            json={"name": "Drive", "batch_year": 2027, "form_schema": {"fields": [{"key": key, "label": label, "type": "text"}]}},
            headers=h(dorg.admin),
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "protected_field", (key, r.text)
    ok = await create_drive(dorg, form_schema={"fields": [{"key": "linkedin", "label": "LinkedIn profile", "type": "text"}, {"key": "cgpa", "label": "CGPA", "type": "text"}]})
    keys = [f["key"] for f in ok["form_schema"]["fields"]]
    assert keys[:6] == ["program", "branch", "cgpa", "percentage", "backlogs", "graduation_year"] and "linkedin" in keys
    assert next(f for f in ok["form_schema"]["fields"] if f["key"] == "cgpa")["type"] == "decimal"


async def test_cross_tenant_and_external_access(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    drive = await create_drive(dorg)
    r = await other_dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}")
    assert r.status_code == 404
    anon = new_client(app)
    assert (await anon.get(f"{CAMPUS}/drives")).status_code == 401
    assert (await anon.get(f"{PUBLIC}/r/{uuid.uuid4().hex * 2}")).status_code == 404
    await anon.aclose()
