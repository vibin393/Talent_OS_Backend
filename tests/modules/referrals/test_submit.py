# ruff: noqa: E501
"""Epic 42 story 42.2 (FR-214, FR-21): upload channel (no processing before acceptance), accept / decline,
duplicates (active vs dormant Candidate), share links (multi-use, bot challenge, rate limit), withdraw, expiry and
linking a general referral to a Demand."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core import bot_challenge
from app.core.settings import get_settings
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import add_application, create_candidate
from tests.modules.referrals.helpers import (
    API,
    ME,
    PDF,
    PUBLIC,
    TOKEN_RE,
    accept,
    create_program,
    employee,
    execute,
    refer,
    refer_ok,
    referee_token,
    rows,
    scalar,
    senior_demand,
)


async def _setup(app: FastAPI, org: DOrg) -> tuple[Any, dict[str, Any]]:
    await create_program(org)
    return await employee(app, org), await senior_demand(org)


async def test_upload_creates_nothing_before_acceptance_then_accept_processes(app: FastAPI, dorg: DOrg) -> None:
    emp, demand = await _setup(app, dorg)
    email = f"priya-{uuid.uuid4().hex[:6]}@friends.example.com"
    out = await refer_ok(emp, demand["id"], email=email, relationship="Former colleague")
    assert out["status_label"] == "Received" and out["can_withdraw"] is True
    assert out["human_id"].startswith("REF-") and out["demand_title"] == demand["title"]
    rid = out["id"]
    # Before acceptance: the referral row and the stored file only -- no Candidate, no application, no consent.
    assert await scalar(dorg, "SELECT count(*) FROM candidate_emails WHERE email = :e", e=email) == 0
    assert await scalar(dorg, "SELECT count(*) FROM applications WHERE source = 'referral'") == 0
    (status, file_id, cand) = (await rows(dorg, "SELECT status, resume_file_id, candidate_id FROM referrals WHERE id = :i", i=rid))[0]
    assert status == "submitted" and file_id is not None and cand is None
    token = referee_token(email)
    view = await emp.client.get(f"{PUBLIC}/{token}/referral")
    assert view.status_code == 200
    v = view.json()
    assert v["mode"] == "accept" and v["referrer_first_name"] == "Esha" and v["job_title"] == demand["title"]
    assert v["notice"]["notice_id"] and "referred you" in v["notice"]["body_md"]
    frame = await emp.client.get(f"{PUBLIC}/{token}")
    assert frame.status_code == 200 and frame.json()["purpose"] == "referral_link"

    r = await accept(emp.client, token)
    assert r.status_code == 200, r.text and r.json()["status"] == "accepted"
    ref = (await rows(dorg, "SELECT status, candidate_id, application_id, consent_id, reward_eligible FROM referrals WHERE id = :i", i=rid))[0]
    assert ref[0] == "in_process" and ref[1] is not None and ref[2] is not None and ref[3] is not None and ref[4] is True
    app_row = (await rows(dorg, "SELECT source, source_ref, candidate_id FROM applications WHERE id = :i", i=ref[2]))[0]
    assert app_row == ("referral", rid, ref[1])
    actor = await scalar(dorg, "SELECT actor_kind FROM application_stage_history WHERE application_id = :i ORDER BY at LIMIT 1", i=ref[2])
    assert actor == "candidate"
    consent = (await rows(dorg, "SELECT purpose, source, candidate_id, evidence->>'referral_id' FROM consents WHERE id = :i", i=ref[3]))[0]
    assert consent == ("referral", "referral_link", ref[1], rid)
    cand = (await rows(dorg, "SELECT source, full_name FROM candidates WHERE id = :i", i=ref[1]))[0]
    assert cand == ("referral", "Priya Sharma")
    assert await scalar(dorg, "SELECT count(*) FROM candidate_resumes WHERE candidate_id = :c", c=ref[1]) == 1
    again = await emp.client.post(f"{PUBLIC}/{token}/referral/accept", json={"agreed": True})
    assert again.status_code == 410  # single use
    mine = (await emp.client.get(ME)).json()
    assert [m["status_label"] for m in mine] == ["In process"] and mine[0]["can_withdraw"] is False


async def test_decline_minimises_and_link_is_single_use(app: FastAPI, dorg: DOrg) -> None:
    emp, demand = await _setup(app, dorg)
    email = f"dev-{uuid.uuid4().hex[:6]}@friends.example.com"
    out = await refer_ok(emp, demand["id"], email=email, note="Great engineer", referee_phone="+919876543210")
    token = referee_token(email)
    r = await emp.client.post(f"{PUBLIC}/{token}/referral/decline")
    assert r.status_code == 200 and r.json()["status"] == "declined"
    row = (await rows(dorg, "SELECT status, referee_name, referee_email, referee_phone, note, resume_file_id FROM referrals WHERE id = :i", i=out["id"]))[0]
    assert row[0] == "declined" and row[1:5] == (None, None, None, None)
    assert await scalar(dorg, "SELECT deleted_at IS NOT NULL FROM stored_files WHERE id = :i", i=row[5]) is True
    assert (await emp.client.post(f"{PUBLIC}/{token}/referral/decline")).status_code == 410
    assert (await emp.client.get(ME)).json()[0]["status_label"] == "Not selected"
    # A declined referral does not block a new referral of the same person.
    assert (await refer(emp.client, demand["id"], headers=emp.h(), email=email)).status_code == 201


async def test_same_referee_twice_for_one_demand_409(app: FastAPI, dorg: DOrg) -> None:
    emp, demand = await _setup(app, dorg)
    email = f"twice-{uuid.uuid4().hex[:6]}@friends.example.com"
    await refer_ok(emp, demand["id"], email=email)
    other = await employee(app, dorg, "Omar Other")
    r = await refer(other.client, demand["id"], headers=other.h(), email=email.upper())
    assert r.status_code == 409 and r.json()["error"]["code"] == "referral_exists"


async def _existing_candidate(org: DOrg, email: str, months_ago: int) -> uuid.UUID:
    cand = await create_candidate(org.admin, "Priya Sharma", email=email)
    other_demand = await senior_demand(org, title="Old opening")
    app_id = await add_application(org.admin, uuid.UUID(other_demand["id"]), cand)
    when = f"now() - interval '{months_ago} months'"
    await execute(org, f"UPDATE applications SET created_at = {when} WHERE id = :i", i=app_id)
    await execute(org, f"UPDATE application_stage_history SET at = {when} WHERE application_id = :i", i=app_id)
    await execute(org, f"UPDATE activities SET occurred_at = {when} WHERE entity_id = :c", c=cand)
    return cand


@pytest.mark.parametrize(("months", "duplicate"), [(3, True), (8, False)])
async def test_duplicate_active_vs_dormant(app: FastAPI, dorg: DOrg, months: int, duplicate: bool) -> None:
    emp, demand = await _setup(app, dorg)
    email = f"known-{uuid.uuid4().hex[:6]}@friends.example.com"
    cand = await _existing_candidate(dorg, email, months)
    out = await refer_ok(emp, demand["id"], email=email)
    assert (await accept(emp.client, referee_token(email))).status_code == 200
    row = (await rows(dorg, "SELECT status, reward_eligible, ineligible_reason, candidate_id, application_id FROM referrals WHERE id = :i", i=out["id"]))[0]
    assert row[3] == cand  # linked to the existing Candidate either way (no new Candidate)
    assert await scalar(dorg, "SELECT count(*) FROM candidate_emails WHERE email = :e", e=email) == 1
    if duplicate:
        assert row[:3] == ("duplicate", False, "duplicate") and row[4] is None
        n = await scalar(dorg, "SELECT count(*) FROM applications WHERE candidate_id = :c AND demand_id = :d", c=cand, d=demand["id"])
        assert n == 0
        assert (await emp.client.get(ME)).json()[0]["status_label"] == "Not selected"
    else:
        assert row[:3] == ("in_process", True, None) and row[4] is not None
        assert await scalar(dorg, "SELECT count(*) FROM candidate_resumes WHERE candidate_id = :c", c=cand) == 1


async def test_share_link_multi_use_bot_challenge_and_rate_limit(
    app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    emp, demand = await _setup(app, dorg)
    first = await emp.client.post(f"{ME}/share-links", json={"demand_id": demand["id"]}, headers=emp.h())
    assert first.status_code == 200, first.text
    second = await emp.client.post(f"{ME}/share-links", json={"demand_id": demand["id"]}, headers=emp.h())
    assert second.json()["url"] == first.json()["url"]  # one active link per (referrer, demand)
    general = await emp.client.post(f"{ME}/share-links", json={}, headers=emp.h())
    assert general.json()["url"] != first.json()["url"]
    m = TOKEN_RE.search(first.json()["url"])
    assert m is not None
    token = m.group(1)
    view = (await emp.client.get(f"{PUBLIC}/{token}/referral")).json()
    assert view["mode"] == "submit" and view["general"] is False
    nid = view["notice"]["notice_id"]

    async def submit(email: str, **extra: Any) -> Any:
        data = {"name": "Kiran Rao", "email": email, "agreed": "true", "notice_id": nid, **extra}
        return await emp.client.post(
            f"{PUBLIC}/{token}/referral", data=data, files={"resume": ("cv.pdf", PDF, "application/pdf")}
        )

    no_consent = await submit(f"k1-{uuid.uuid4().hex[:5]}@x.example.com", agreed="false")
    assert no_consent.status_code == 422
    for i in range(2):
        r = await submit(f"k{i}-{uuid.uuid4().hex[:5]}@x.example.com")
        assert r.status_code == 200, r.text
    got = await rows(dorg, "SELECT status, channel FROM referrals WHERE share_link_id IS NOT NULL ORDER BY created_at")
    assert got == [("in_process", "share_link"), ("in_process", "share_link")]
    assert await scalar(dorg, "SELECT uses FROM referral_share_links WHERE demand_id = :d", d=demand["id"]) == 2

    class Refuse:
        async def verify(self, token: str | None, remote_ip: str) -> bool:
            return False

    bot_challenge.set_verifier(Refuse())
    try:
        bot = await submit(f"bot-{uuid.uuid4().hex[:5]}@x.example.com")
        assert bot.status_code == 422 and bot.json()["error"]["code"] == "challenge_failed"
    finally:
        bot_challenge.set_verifier(None)

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    codes = [(await submit(f"rl{i}-{uuid.uuid4().hex[:5]}@x.example.com", agreed="false")).status_code for i in range(12)]
    assert 429 in codes and codes[0] == 422


async def test_withdraw_only_while_submitted(app: FastAPI, dorg: DOrg) -> None:
    emp, demand = await _setup(app, dorg)
    out = await refer_ok(emp, demand["id"])
    r = await emp.client.post(f"{ME}/{out['id']}/withdraw", headers=emp.h())
    assert r.status_code == 200 and r.json()["status_label"] == "Not selected"
    assert await scalar(dorg, "SELECT status FROM referrals WHERE id = :i", i=out["id"]) == "withdrawn"
    assert (await emp.client.post(f"{ME}/{out['id']}/withdraw", headers=emp.h())).status_code == 409
    other = await employee(app, dorg, "Omar Other")
    assert (await other.client.post(f"{ME}/{out['id']}/withdraw", headers=other.h())).status_code == 404


async def test_expiry_minimises(app: FastAPI, dorg: DOrg) -> None:
    from app.tasks.referrals import expire_tenant

    emp, demand = await _setup(app, dorg)
    out = await refer_ok(emp, demand["id"])
    await execute(
        dorg,
        "UPDATE secure_links SET expires_at = now() - interval '1 minute' WHERE id = (SELECT secure_link_id FROM referrals WHERE id = :i)",
        i=out["id"],
    )
    assert await expire_tenant(dorg.tenant_id) == 1
    row = (await rows(dorg, "SELECT status, referee_email FROM referrals WHERE id = :i", i=out["id"]))[0]
    assert row == ("expired", None)


async def test_general_referral_then_link_demand(app: FastAPI, dorg: DOrg) -> None:
    emp, demand = await _setup(app, dorg)
    email = f"gen-{uuid.uuid4().hex[:6]}@friends.example.com"
    out = await refer_ok(emp, None, email=email)
    assert out["demand_title"] is None
    assert (await accept(emp.client, referee_token(email))).status_code == 200
    row = (await rows(dorg, "SELECT status, reward_eligible, ineligible_reason FROM referrals WHERE id = :i", i=out["id"]))[0]
    assert row == ("accepted", False, "no_program")
    r = await dorg.admin.client.post(
        f"{API}/referrals/{out['id']}/link-demand", json={"demand_id": demand["id"]}, headers=h(dorg.admin)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_process" and body["reward_eligible"] is True and body["application_id"]
    src = await scalar(dorg, "SELECT source_ref FROM applications WHERE id = :i", i=body["application_id"])
    assert src == out["id"]
    again = await dorg.admin.client.post(
        f"{API}/referrals/{out['id']}/link-demand", json={"demand_id": demand["id"]}, headers=h(dorg.admin)
    )
    assert again.status_code == 409
