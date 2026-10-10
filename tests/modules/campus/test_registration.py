# ruff: noqa: E501
"""Epic 43 story 43.2: public registration (validation, notice, consent, rate limits, duplicates) and imports."""

import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.core import rate_limit
from tests.epic12 import sent
from tests.modules.campus.helpers import (
    CAMPUS,
    PUBLIC,
    answers,
    ready_drive,
    register,
    registrations,
    rows,
    scalar,
    token,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import new_client

LINK_RE = re.compile(r"/campus/r/([A-Za-z0-9_\-]+)")


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    client = new_client(app)
    yield client
    await client.aclose()


async def test_public_view_and_registration(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    tok = token(drive["institutions"][0]["registration_url"])
    view = (await anon.get(f"{PUBLIC}/r/{tok}")).json()
    assert view["status"] == "open" and view["drive_name"] == "Campus 2027"
    program = next(f for f in view["fields"] if f["key"] == "program")
    assert program["options"] == ["B.Tech CS", "B.Tech IT", "B.Tech ME"]
    assert view["notice"]["notice_id"] and view["notice"]["title"]
    r = await register(anon, tok, email="ravi@college.example.edu")
    assert r.status_code == 200 and r.json()["status"] == "registered", r.text
    regs = await registrations(dorg, drive["id"])
    assert len(regs) == 1
    reg = regs[0]
    assert reg["status"] == "eligible" and reg["source"] == "link" and reg["cgpa"] == "8.10" and reg["graduation_year"] == 2027
    consents = await rows(dorg, "SELECT purpose, source FROM consents WHERE candidate_id = :c", c=reg["candidate_id"])
    assert [(p, s) for p, s in consents] == [("campus", "campus_registration")]
    assert await scalar(dorg, "SELECT source FROM candidates WHERE id = :c", c=reg["candidate_id"]) == "campus"
    again = await register(anon, tok, email="RAVI@college.example.edu", marketing_opt_in=True)
    assert again.json()["status"] == "already_registered"
    assert len(await registrations(dorg, drive["id"])) == 1


async def test_validation_errors_per_field(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    tok = token(drive["institutions"][0]["registration_url"])
    r = await register(anon, tok, ans={"program": "MBA", "cgpa": "11", "backlogs": "x", "nope": 1})
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["details"]["fields"]
    assert set(fields) >= {"program", "cgpa", "backlogs", "graduation_year", "nope"}


async def test_notice_outdated_and_consent_required(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    tok = token(drive["institutions"][0]["registration_url"])
    stale = await register(anon, tok, notice_id=str(uuid.uuid4()))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "notice_outdated"
    no = await register(anon, tok, agreed=False)
    assert no.status_code == 422 and no.json()["error"]["code"] == "consent_required"


async def test_marketing_consent_only_when_ticked(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    tok = token(drive["institutions"][0]["registration_url"])
    await register(anon, tok, email="a@college.example.edu", marketing_opt_in=True)
    await register(anon, tok, email="b@college.example.edu")
    regs = {r["candidate_email"]: r for r in await registrations(dorg, drive["id"])}
    a = await rows(dorg, "SELECT purpose FROM consents WHERE candidate_id = :c ORDER BY purpose", c=regs["a@college.example.edu"]["candidate_id"])
    b = await rows(dorg, "SELECT purpose FROM consents WHERE candidate_id = :c ORDER BY purpose", c=regs["b@college.example.edu"]["candidate_id"])
    assert [x[0] for x in a] == ["campus", "marketing"] and [x[0] for x in b] == ["campus"]


async def test_existing_candidate_linked_and_fuzzy_duplicate_queued(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    existing = await dorg.admin.client.post(f"{API_C}", json={"full_name": "Priya Raman", "emails": [{"email": "priya@college.example.edu"}], "phones": [{"phone": "+919840011111"}]}, headers=h(dorg.admin))
    assert existing.status_code == 201, existing.text
    drive = await ready_drive(dorg)
    tok = token(drive["institutions"][0]["registration_url"])
    await register(anon, tok, email="priya@college.example.edu", name="Priya Raman")
    regs = await registrations(dorg, drive["id"])
    assert regs[0]["candidate_id"] == existing.json()["id"]
    await register(anon, tok, email="priya.r@other.example.com", name="Priya Raman", phone="+919840011111")
    pairs = await scalar(dorg, "SELECT count(*) FROM candidate_duplicates WHERE status = 'open'")
    assert pairs >= 1
    assert await scalar(dorg, "SELECT count(*) FROM candidates WHERE merged_into_id IS NOT NULL") == 0


API_C = "/api/v1/candidates"


async def test_rate_limits_per_ip_and_drive(dorg: DOrg, anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.settings import get_settings

    drive = await ready_drive(dorg)
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    tok = token(drive["institutions"][0]["registration_url"])
    r = await dorg.admin.client.put(f"{CAMPUS}/settings", json={"register_ip_limit": 2, "register_drive_limit": 100, "avg_interview_minutes": 20}, headers=h(dorg.admin))
    assert r.status_code == 200
    assert (await register(anon, tok)).status_code == 200
    assert (await register(anon, tok)).status_code == 200
    limited = await register(anon, tok)
    assert limited.status_code == 429
    rate_limit.memory_backend().reset()
    for _ in range(100):
        await rate_limit.enforce("campus_register_drive", uuid.UUID(drive["id"]).hex, 100, 600)
    assert (await register(anon, tok)).status_code == 429


async def test_import_preview_apply_invites_only(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    di = drive["institutions"][0]["id"]
    csv = "email,name,cgpa\nstu1@college.example.edu,Stu One,8.5\n,No Mail,7\nbad-email,Bad,7\nstu1@college.example.edu,Dup,7\nstu2@college.example.edu,Stu Two,12\nstu3@college.example.edu,Stu Three,\n"
    r = await dorg.admin.client.post(
        f"{CAMPUS}/drives/{drive['id']}/imports/preview",
        data={"drive_institution_id": di},
        files={"file": ("list.csv", csv.encode(), "text/csv")},
        headers=h(dorg.admin),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["row_count"] == 6 and out["error_count"] == 4
    errors = {row["row"]: row["errors"] for row in out["rows"]}
    assert errors[2] == [] and errors[7] == [] and "email: missing" in errors[3] and any("duplicate" in e for e in errors[5])
    before = await scalar(dorg, "SELECT count(*) FROM candidates")
    applied = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/imports/{out['batch_id']}/apply", headers=h(dorg.admin))
    assert applied.status_code == 200 and applied.json()["sent"] == 2, applied.text
    assert await scalar(dorg, "SELECT count(*) FROM candidates") == before
    assert await scalar(dorg, "SELECT count(*) FROM consents") == 0
    mail = sent(kind="campus_registration_invite", to="stu1@college.example.edu")
    assert mail
    m = LINK_RE.search(mail[-1].text)
    assert m is not None
    view = (await anon.get(f"{PUBLIC}/r/{m.group(1)}")).json()
    assert view["prefill"]["email"] == "stu1@college.example.edu" and view["prefill"]["cgpa"] == "8.50"
    reg = await register(anon, m.group(1), email="stu1@college.example.edu")
    assert reg.status_code == 200, reg.text
    regs = await registrations(dorg, drive["id"])
    assert regs[0]["source"] == "import"
    used = await anon.get(f"{PUBLIC}/r/{m.group(1)}")
    assert used.status_code == 410
    twice = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/imports/{out['batch_id']}/apply", headers=h(dorg.admin))
    assert twice.status_code == 409


async def test_answers_file_field(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    import json

    from tests.epic3 import make_pdf

    drive = await ready_drive(dorg, form_schema={"fields": [{"key": "resume", "label": "Resume", "type": "file", "required": True}]})
    tok = token(drive["institutions"][0]["registration_url"])
    view = (await anon.get(f"{PUBLIC}/r/{tok}")).json()
    payload = {"full_name": "File Person", "email": "file@college.example.edu", "answers": answers(), "notice_id": view["notice"]["notice_id"], "agreed": True}
    missing = await anon.post(f"{PUBLIC}/r/{tok}", data={"payload": json.dumps(payload)}, files={"x": ("a.txt", b"x", "text/plain")})
    assert missing.status_code == 422 and "resume" in missing.json()["error"]["details"]["fields"]
    ok = await anon.post(f"{PUBLIC}/r/{tok}", data={"payload": json.dumps(payload)}, files={"file_resume": ("cv.pdf", make_pdf("resume"), "application/pdf")})
    assert ok.status_code == 200, ok.text
    reg = (await registrations(dorg, drive["id"]))[0]
    assert await scalar(dorg, "SELECT owner_entity_type FROM stored_files WHERE id = :i", i=reg["answers"]["resume"]) == "campus_registration"
