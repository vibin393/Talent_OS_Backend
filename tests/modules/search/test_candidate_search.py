"""Story 5.1: Boolean / faceted candidate search over the ``candidate_search_docs`` projection."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.search import CandidateSearchDoc
from tests.epic5 import DATA_ENGINEER, facet, h, make_candidate, names, patch_candidate, search, search_ok
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import join


async def _seed(org: DOrg) -> dict[str, dict[str, Any]]:
    m = org.admin
    out = {}
    out["priya"] = await make_candidate(m, "Priya Raman", **DATA_ENGINEER, tags=["hot"])
    out["jose"] = await make_candidate(
        m,
        "José Álvarez",
        current_title="Data Engineer",
        location={"city": "Pune", "country": "India"},
        total_experience_months=96,
        skills=[{"name": "Databricks", "years": 6}, "SAS"],
        notice_period_days=90,
    )
    out["anita"] = await make_candidate(
        m,
        "Anita Desai",
        current_title="Java Developer",
        location={"city": "Chennai", "country": "India"},
        total_experience_months=24,
        skills=["Java", "Spring Boot"],
        certifications=[{"name": "OCP Java"}],
        expected_ctc_amount="1800000",
        expected_ctc_currency="INR",
        expected_ctc_unit="annual",
    )
    return out


async def test_projection_rows_are_written_on_create(dorg: DOrg) -> None:
    c = await make_candidate(dorg.admin, "Émilie Durand", **DATA_ENGINEER)
    async with tenant_session(dorg.tenant_id) as db:
        doc = (
            await db.execute(
                select(CandidateSearchDoc).where(CandidateSearchDoc.candidate_id == uuid.UUID(c["id"]))
            )
        ).scalar_one()
    assert doc.human_id == c["human_id"]
    assert doc.name_norm == "emilie durand"
    assert set(doc.skill_names) >= {"Databricks", "PySpark"}
    assert doc.city == "Chennai" and doc.experience_years == 5
    assert doc.is_archived is False


async def test_boolean_semantics(dorg: DOrg) -> None:
    await _seed(dorg)
    m = dorg.admin
    res = await search_ok(m, q='("data engineer" OR dataengineer) AND databricks NOT sas')
    assert names(res) == ["Priya Raman"]
    res = await search_ok(m, q="databricks")
    assert set(names(res)) == {"Priya Raman", "José Álvarez"}
    res = await search_ok(m, q="java OR pyspark")
    assert set(names(res)) == {"Priya Raman", "Anita Desai"}
    res = await search_ok(m, q="databrick*")
    assert set(names(res)) == {"Priya Raman", "José Álvarez"}
    res = await search_ok(m, q="-sas databricks")
    assert names(res) == ["Priya Raman"]
    # lowercase "and"/"or" are terms, adjacent atoms are an implicit AND
    res = await search_ok(m, q="databricks pyspark")
    assert names(res) == ["Priya Raman"]


async def test_unaccent_and_trigram_name(dorg: DOrg) -> None:
    await _seed(dorg)
    m = dorg.admin
    assert names(await search_ok(m, q="jose")) == ["José Álvarez"]
    assert names(await search_ok(m, q="alvarez")) == ["José Álvarez"]
    # misspelt name: trigram similarity fallback for single-atom queries
    assert "Priya Raman" in names(await search_ok(m, q='"Priya Ramen"')) or "Priya Raman" in names(
        await search_ok(m, q="Ramann")
    )


async def test_invalid_syntax_is_a_422_with_position(dorg: DOrg) -> None:
    resp = await search(dorg.admin.client, q="databricks AND (azure")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_query"
    assert err["details"] == {"position": 16, "message": "Unclosed parenthesis"}
    for bad in ('"open quote', "AND x", "x OR", "NOT sas", "()", "a )"):
        resp = await search(dorg.admin.client, q=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "invalid_query"


async def test_injection_strings_are_plain_terms(dorg: DOrg) -> None:
    await _seed(dorg)
    for q in ("x' | 'y", "a:* & !b", "databricks'); DROP TABLE candidates;--", "<->", "java\\"):
        resp = await search(dorg.admin.client, q=q)
        assert resp.status_code in {200, 422}, (q, resp.text)
    assert len(names(await search_ok(dorg.admin, q="databricks"))) == 2


async def test_filters_and_facets(dorg: DOrg) -> None:
    seeded = await _seed(dorg)
    m = dorg.admin
    res = await search_ok(m, filters={"cities": ["chennai"]})
    assert set(names(res)) == {"Priya Raman", "Anita Desai"}
    # the city facet ignores its own filter, the others apply it
    assert facet(res, "city") == {"Chennai": 2, "Pune": 1}
    assert facet(res, "skills")["Databricks"] == 1
    res = await search_ok(m, filters={"experience_min": 4, "experience_max": 10})
    assert set(names(res)) == {"Priya Raman", "José Álvarez"}
    assert (
        facet(res, "experience") == {"0-2": 0, "2-5": 1, "5-8": 1, "8-12": 1}
        or sum(facet(res, "experience").values()) == 3
    )
    res = await search_ok(m, filters={"notice_max_days": 30})
    assert names(res) == ["Priya Raman"]
    res = await search_ok(m, filters={"certifications": ["OCP Java"]})
    assert names(res) == ["Anita Desai"]
    res = await search_ok(m, filters={"sources": ["manual"], "owner_ids": [str(m.user_id)]})
    assert len(res["items"]) == 3
    tag_id = next(t["id"] for t in seeded["priya"]["tags"])
    res = await search_ok(m, filters={"tags": [tag_id]})
    assert names(res) == ["Priya Raman"]
    assert facet(res, "tags") == {"hot": 1}
    res = await search_ok(m, filters={"comp_max": 2000000})
    assert names(res) == ["Anita Desai"]
    res = await search_ok(m, filters={"current_employers": ["hdfc bank"]})
    assert names(res) == ["Priya Raman"]
    res = await search_ok(m, filters={"available_by": "2099-01-01"})
    assert set(names(res)) == {"Priya Raman", "José Álvarez"}  # notice-based availability


async def test_skill_filter_expands_children_and_all_mode(dorg: DOrg) -> None:
    await _seed(dorg)
    m = dorg.admin
    skills = (await m.client.get("/api/v1/skills", params={"q": "Apache Spark"})).json()
    spark = next(
        s for s in (skills["items"] if isinstance(skills, dict) else skills) if s["name"] == "Apache Spark"
    )
    res = await search_ok(m, filters={"skills": [spark["id"]]})
    assert names(res) == ["Priya Raman"]  # PySpark is a child of Apache Spark
    java = (await m.client.get("/api/v1/skills", params={"q": "Java"})).json()
    java_id = next(s for s in (java["items"] if isinstance(java, dict) else java) if s["name"] == "Java")[
        "id"
    ]
    res = await search_ok(m, filters={"skills": [spark["id"], java_id], "skills_mode": "all"})
    assert names(res) == []
    res = await search_ok(m, filters={"skills": [spark["id"], java_id], "skills_mode": "any"})
    assert set(names(res)) == {"Priya Raman", "Anita Desai"}


async def test_comp_filter_needs_compensation_view(app: FastAPI, dorg: DOrg) -> None:
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    resp = await search(recruiter.client, filters={"comp_max": 100})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden_filter"
    assert (await search(recruiter.client, q="x")).status_code == 200
    await recruiter.client.aclose()


async def test_scope_own_and_archived_excluded(app: FastAPI, dorg: DOrg) -> None:
    seeded = await _seed(dorg)
    viewer = await join(
        app, dorg.tenant, "interviewer", name="Ivan Interviewer"
    )  # candidate:read at own scope
    mine = await make_candidate(
        dorg.admin, "Owned Byivan", owner_id=str(viewer.user_id), skills=["Databricks"]
    )
    res = await search_ok(viewer, q="databricks")
    assert names(res) == ["Owned Byivan"]
    resp = await dorg.admin.client.post(
        f"/api/v1/candidates/{seeded['jose']['id']}/archive", headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    res = await search_ok(dorg.admin, q="databricks")
    assert set(names(res)) == {"Priya Raman", "Owned Byivan"}
    assert mine["id"]
    await viewer.client.aclose()


async def test_update_refreshes_projection_and_sorts(dorg: DOrg) -> None:
    seeded = await _seed(dorg)
    m = dorg.admin
    await patch_candidate(m, seeded["anita"]["id"], skills=["Java", "Databricks"])
    res = await search_ok(m, q="databricks", sort="experience")
    assert names(res) == ["José Álvarez", "Priya Raman", "Anita Desai"]
    res = await search_ok(m, sort="updated", page_size=1)
    assert res["total"] == 3 and names(res) == ["Anita Desai"]


async def test_snippets_are_segments_without_html(dorg: DOrg) -> None:
    await make_candidate(
        dorg.admin, "Snip Pet", current_title="<b>Data</b> Engineer <script>x</script>", skills=["Go"]
    )
    res = await search_ok(dorg.admin, q="engineer")
    item = res["items"][0]
    segments = item["snippet"]
    assert segments and any(s["hl"] for s in segments)
    joined = "".join(s["text"] for s in segments)
    assert "\x01" not in joined and "\x02" not in joined
    assert all(set(s) == {"text", "hl"} for s in segments)


async def test_cross_tenant_isolation(dorg: DOrg, other_dorg: DOrg) -> None:
    await _seed(dorg)
    res = await search_ok(other_dorg.admin, q="databricks")
    assert res["total"] == 0
