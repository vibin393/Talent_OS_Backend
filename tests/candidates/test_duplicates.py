"""Story 3.5: duplicate detection (exact + fuzzy), review queue, keep separate, link_by_exact_email."""

from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services.candidates import link_by_exact_email
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, member

DUPS = f"{CANDIDATES}/duplicates"


async def test_exact_email_flags_open_item(org3: Org3) -> None:
    first = await create_candidate(org3.admin, full_name="Anil Kumar", emails=[{"email": "anil@example.com"}])
    second = await create_candidate(org3.admin, full_name="A. Kumar", emails=[{"email": "ANIL@example.com"}])
    assert [d["id"] for d in second["warnings"]["possible_duplicates"]] == [first["id"]]
    queue = (await org3.admin.client.get(DUPS, params={"status": "open"})).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["reasons"] == ["exact_email"]
    assert {item["candidate_a"]["id"], item["candidate_b"]["id"]} == {first["id"], second["id"]}
    assert item["score"] == "1.000"
    assert (await org3.admin.client.get(f"{CANDIDATES}/{first['id']}")).json()["counts"][
        "open_duplicates"
    ] == 1


async def test_exact_phone_and_link(org3: Org3) -> None:
    await create_candidate(org3.admin, full_name="P One", phones=[{"phone": "+91 98765 00001"}])
    second = await create_candidate(org3.admin, full_name="Q Two", phones=[{"phone": "9876500001"}])
    assert len(second["warnings"]["possible_duplicates"]) == 1
    await create_candidate(org3.admin, full_name="L One", links=[{"url": "github.com/lone"}])
    third = await create_candidate(
        org3.admin, full_name="L Two", links=[{"url": "https://www.GitHub.com/lone/"}]
    )
    assert len(third["warnings"]["possible_duplicates"]) == 1
    reasons = sorted(i["reasons"][0] for i in (await org3.admin.client.get(DUPS)).json()["items"])
    assert reasons == ["exact_link", "exact_phone"]


async def test_fuzzy_name_and_employer(org3: Org3) -> None:
    await create_candidate(org3.admin, full_name="Rahul Sharma", current_employer="Infosys")
    fuzzy = await create_candidate(org3.admin, full_name="Rahul Sharmaa", current_employer="Infosys Ltd")
    assert len(fuzzy["warnings"]["possible_duplicates"]) == 1
    item = (await org3.admin.client.get(DUPS)).json()["items"][0]
    assert "fuzzy_name_employer" in item["reasons"]
    different = await create_candidate(org3.admin, full_name="Meera Nair", current_employer="Infosys")
    assert different["warnings"]["possible_duplicates"] == []


async def test_fuzzy_name_and_city(org3: Org3) -> None:
    await create_candidate(org3.admin, full_name="Kavya Menon", location={"city": "Kochi"})
    again = await create_candidate(org3.admin, full_name="Kavya Menon", location={"city": "KOCHI"})
    assert len(again["warnings"]["possible_duplicates"]) == 1
    item = (await org3.admin.client.get(DUPS)).json()["items"][0]
    assert item["reasons"] == ["fuzzy_name_city"]


async def test_keep_separate_is_never_reflagged(org3: Org3) -> None:
    a = await create_candidate(org3.admin, full_name="Same Name", emails=[{"email": "same@example.com"}])
    await create_candidate(org3.admin, full_name="Same Name", emails=[{"email": "same@example.com"}])
    item = (await org3.admin.client.get(DUPS)).json()["items"][0]
    detail = (await org3.admin.client.get(f"{DUPS}/{item['id']}")).json()
    assert detail["profile_a"]["full_name"] == "Same Name"
    assert "owner_id" not in detail["differing_fields"]
    kept = await org3.admin.client.post(f"{DUPS}/{item['id']}/keep-separate", headers=h(org3.admin))
    assert kept.status_code == 200, kept.text
    assert kept.json()["status"] == "kept_separate"
    resave = await org3.admin.client.patch(
        f"{CANDIDATES}/{a['id']}", json={"current_title": "Changed"}, headers=h(org3.admin)
    )
    assert resave.status_code == 200
    assert (await org3.admin.client.get(DUPS, params={"status": "open"})).json()["total"] == 0


async def test_merge_permission_required_for_resolution(app: FastAPI, org3: Org3) -> None:
    await create_candidate(org3.admin, full_name="Dup A", emails=[{"email": "dup@example.com"}])
    await create_candidate(org3.admin, full_name="Dup B", emails=[{"email": "dup@example.com"}])
    item = (await org3.admin.client.get(DUPS)).json()["items"][0]
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(DUPS)).json()["total"] == 1
        denied = await recruiter.client.post(f"{DUPS}/{item['id']}/keep-separate", headers=h(recruiter))
        assert denied.status_code == 403
    finally:
        await recruiter.client.aclose()


async def test_link_by_exact_email(org3: Org3) -> None:
    cand = await create_candidate(org3.admin, emails=[{"email": "link.me@example.com"}])
    async with tenant_session(org3.tenant_id) as db:
        ctx = RequestContext(tenant_id=org3.tenant_id)
        assert str(await link_by_exact_email(ctx, db, "Link.Me@Example.com")) == cand["id"]
        assert await link_by_exact_email(ctx, db, "nobody@example.com") is None
