"""Story 3.2: skill catalog seed, synonym resolution, pending tenant skills, approve / reject / merge."""

from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.models.candidates import Skill
from app.services.candidates import resolve_skill
from app.services.candidates.common import normalize_skill
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, member

SKILLS = "/api/v1/skills"


async def test_seed_has_300_plus_skills_and_parent_chain() -> None:
    async with platform_session() as db:
        count = (await db.execute(select(func.count()).select_from(Skill))).scalar_one()
        boot = (await db.execute(select(Skill).where(Skill.slug == "spring-boot"))).scalar_one()
        spring = await db.get(Skill, boot.parent_id)
        assert spring is not None
        java = await db.get(Skill, spring.parent_id)
    assert count >= 300
    assert spring.name == "Spring"
    assert java is not None
    assert java.name == "Java"


async def test_runtime_role_cannot_write_platform_catalog() -> None:
    async with platform_session() as db:
        privileges = (
            await db.execute(
                text(
                    "SELECT has_table_privilege(current_user, 'skills', 'INSERT'),"
                    " has_table_privilege(current_user, 'skill_aliases', 'UPDATE'),"
                    " has_table_privilege(current_user, 'skills', 'SELECT')"
                )
            )
        ).one()
    assert tuple(privileges) == (False, False, True)


def test_normalisation_keeps_plus_hash_dot() -> None:
    assert normalize_skill("  C++ ") == "c++"
    assert normalize_skill("C#") == "c#"
    assert normalize_skill("React.JS") == "react.js"
    assert normalize_skill("Spring    Boot") == "spring boot"


async def test_synonyms_resolve_to_canonical(org3: Org3) -> None:
    ctx = RequestContext(tenant_id=org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        cases = {
            "adf": "Azure Data Factory",
            "Azure Data Factory": "Azure Data Factory",
            "JS": "JavaScript",
            "k8s": "Kubernetes",
            "React.js": "React",
            "MS Excel": "Excel",
            "spring boot": "Spring Boot",
        }
        for raw, expected in cases.items():
            ref = await resolve_skill(ctx, db, raw, create_pending=False)
            assert ref is not None, raw
            assert (ref.kind, ref.name) == ("global", expected), raw
        assert await resolve_skill(ctx, db, "FooLang", create_pending=False) is None


async def test_unknown_skill_creates_pending_then_approve(app: FastAPI, org3: Org3) -> None:
    ctx = RequestContext(tenant_id=org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        ref = await resolve_skill(ctx, db, "FooLang")
        again = await resolve_skill(ctx, db, "foolang")
    assert ref is not None
    assert again is not None
    assert (ref.kind, ref.status, ref.name) == ("tenant", "pending", "FooLang")
    assert again.id == ref.id

    pending = await org3.admin.client.get(f"{SKILLS}/pending")
    assert [p["name"] for p in pending.json()["items"]] == ["FooLang"]

    recruiter = await member(app, org3, "recruiter")
    try:
        denied = await recruiter.client.post(f"{SKILLS}/{ref.id}/approve", headers=h(recruiter))
        assert denied.status_code == 403
        # typeahead is open to candidate readers; pending ones only on request
        found = await recruiter.client.get(SKILLS, params={"q": "foolang", "include_pending": True})
        assert [s["name"] for s in found.json()["items"]] == ["FooLang"]
        assert (await recruiter.client.get(SKILLS, params={"q": "foolang"})).json()["total"] == 0
    finally:
        await recruiter.client.aclose()

    approved = await org3.admin.client.post(f"{SKILLS}/{ref.id}/approve", headers=h(org3.admin))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


async def test_typeahead_returns_global_and_alias_hits(org3: Org3) -> None:
    resp = await org3.admin.client.get(SKILLS, params={"q": "k8s"})
    assert resp.status_code == 200
    assert "Kubernetes" in [s["name"] for s in resp.json()["items"]]
    resp = await org3.admin.client.get(SKILLS, params={"q": "java", "page_size": 5})
    assert resp.json()["items"][0]["name"] == "Java"


async def test_admin_creates_skill_and_alias(org3: Org3) -> None:
    created = await org3.admin.client.post(
        SKILLS, json={"name": "Talent OS Scripting", "aliases": ["TOSS"]}, headers=h(org3.admin)
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["kind"], body["status"]) == ("tenant", "approved")
    dup = await org3.admin.client.post(SKILLS, json={"name": "Java"}, headers=h(org3.admin))
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "skill_exists"

    java = (await org3.admin.client.get(SKILLS, params={"q": "java"})).json()["items"][0]
    alias = await org3.admin.client.post(
        f"{SKILLS}/global/{java['id']}/aliases", json={"alias": "Jawa"}, headers=h(org3.admin)
    )
    assert alias.status_code == 201, alias.text
    ctx = RequestContext(tenant_id=org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        ref = await resolve_skill(ctx, db, "jawa", create_pending=False)
        toss = await resolve_skill(ctx, db, "toss", create_pending=False)
    assert ref is not None
    assert ref.name == "Java"
    assert toss is not None
    assert toss.name == "Talent OS Scripting"
    taken = await org3.admin.client.post(
        f"{SKILLS}/global/{java['id']}/aliases", json={"alias": "jawa"}, headers=h(org3.admin)
    )
    assert taken.status_code == 409


async def test_reject_with_merge_into_repoints_candidate_skills(org3: Org3) -> None:
    cand = await create_candidate(org3.admin, skills=["Pythn", {"name": "Python", "years": 1}])
    pending = (await org3.admin.client.get(f"{SKILLS}/pending")).json()["items"]
    (typo,) = [p for p in pending if p["name"] == "Pythn"]
    assert typo["candidate_count"] == 1
    python = next(s for s in cand["skills"] if s["skill"]["name"] == "Python")["skill"]
    resp = await org3.admin.client.post(
        f"{SKILLS}/{typo['id']}/reject",
        json={"merge_into": {"kind": python["kind"], "id": python["id"]}},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    after = (await org3.admin.client.get(f"{CANDIDATES}/{cand['id']}")).json()
    assert [s["skill"]["name"] for s in after["skills"]] == ["Python"]
    ctx = RequestContext(tenant_id=org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        ref = await resolve_skill(ctx, db, "pythn", create_pending=False)
    assert ref is not None
    assert ref.name == "Python"


def test_cli_seed_is_idempotent() -> None:
    from app.cli import main

    assert main(["seed-skills"]) == 0
