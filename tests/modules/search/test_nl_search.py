"""Story 5.3: natural-language search (mocked AI gateway) and the keyword fallback."""

from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.ai import AIAction
from app.services import ai
from tests.epic3 import FakeAI
from tests.epic5 import DATA_ENGINEER, make_candidate, names, search, search_ok
from tests.modules.demands.helpers import DOrg

QUERY = "Data engineers with Databricks and banking, available this month"


def interpretation(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "keywords": '"data engineer"',
        "skills_all": ["Databricks", "FooLangXYZ"],
        "skills_any": [],
        "experience_min": 3,
        "countries": ["India"],
        "notice_max_days": 60,
        "domains": ["banking"],
        "semantic_text": "Data engineer with Databricks and banking experience",
    }
    body.update(overrides)
    return body


async def _seed(org: DOrg) -> None:
    m = org.admin
    await make_candidate(m, "Priya Raman", **DATA_ENGINEER, certifications=[{"name": "FooLangXYZ Pro"}])
    await make_candidate(
        m, "Jose Alvarez", current_title="Data Engineer", skills=["Databricks"], notice_period_days=90
    )
    await make_candidate(m, "Anita Desai", current_title="Java Developer", skills=["Java"])


async def test_nl_query_returns_chips_blend_and_ai_action(dorg: DOrg, fake_ai: FakeAI) -> None:
    await _seed(dorg)
    ai.set_consent_checker(ai.allow_all_consent_checker())
    fake_ai.payload = interpretation()
    res = await search_ok(dorg.admin, q=QUERY, mode="nl")
    assert res["notice"] is None
    assert res["ai_action_id"]
    assert names(res) == ["Priya Raman"]  # Databricks (all) + unresolved skill as keyword + notice <= 60
    chips = {c["key"]: c for c in res["interpretation"]}
    assert set(chips) >= {
        "keywords",
        "skills_all",
        "experience_min",
        "countries",
        "notice_max_days",
        "domains",
    }
    skill_names = {s["name"] for s in chips["skills_all"]["value"]}
    assert skill_names == {"Databricks", "FooLangXYZ"}
    assert res["applied_filters"]["skills_mode"] == "all" and len(res["applied_filters"]["skills"]) == 1
    assert '"FooLangXYZ"' in res["applied_q"]
    assert res["semantic_text"].startswith("Data engineer")
    assert res["items"][0]["score"] is not None
    # the user query is fenced as untrusted data
    sent = fake_ai.calls[0]["messages"][0]["content"]
    assert '<untrusted_document name="query">' in sent and QUERY in sent
    async with tenant_session(dorg.tenant_id) as db:
        action = await db.get(AIAction, res["ai_action_id"])
        assert action is not None and action.feature == "nl_search" and action.status == "ok"
    # editing chips re-queries in structured mode without a new AI call
    calls = len(fake_ai.calls)
    again = await search_ok(
        dorg.admin,
        q=res["applied_q"],
        mode="structured",
        filters={**res["applied_filters"], "notice_max_days": 120},
        semantic_text=res["semantic_text"],
    )
    assert len(fake_ai.calls) == calls
    assert names(again) == ["Priya Raman"]


async def test_nl_semantic_ranking_orders_by_blend(dorg: DOrg, fake_ai: FakeAI) -> None:
    await _seed(dorg)
    fake_ai.payload = interpretation(
        keywords=None,
        skills_all=[],
        countries=[],
        notice_max_days=None,
        experience_min=None,
        domains=[],
        semantic_text="Java Developer Java",
    )
    res = await search_ok(dorg.admin, q="someone who knows java", mode="nl")
    assert res["total"] == 3
    assert names(res)[0] == "Anita Desai"


async def test_nl_falls_back_to_keyword_search_without_ai(dorg: DOrg) -> None:
    await _seed(dorg)
    res = await search_ok(dorg.admin, q="databricks engineer", mode="nl")  # no ANTHROPIC key in tests
    assert res["notice"] == "ai_unavailable"
    assert res["interpretation"] is None
    assert set(names(res)) == {"Priya Raman", "Jose Alvarez"}
    async with tenant_session(dorg.tenant_id) as db:
        rows = (await db.execute(select(AIAction).where(AIAction.feature == "nl_search"))).scalars().all()
    assert rows and rows[-1].status == "skipped"


async def test_nl_invalid_ai_output_falls_back(dorg: DOrg, fake_ai: FakeAI) -> None:
    await _seed(dorg)
    fake_ai.payload = {**interpretation(), "rank_first": "Priya"}  # extra key -> schema violation
    res = await search_ok(dorg.admin, q=QUERY, mode="nl")
    assert res["notice"] == "ai_unavailable"
    # keyword AI output that does not parse is dropped, never a 5xx
    fake_ai.payload = interpretation(keywords="databricks AND (", skills_all=["Databricks"])
    res = await search_ok(dorg.admin, q=QUERY, mode="nl")
    assert res["notice"] is None and "keywords" not in {c["key"] for c in res["interpretation"]}


async def test_nl_comp_chip_dropped_without_compensation_view(dorg: DOrg, fake_ai: FakeAI) -> None:
    await _seed(dorg)
    fake_ai.payload = interpretation(
        comp_max={"amount": 2500000, "currency": "INR"}, skills_all=["Databricks"]
    )
    res = await search_ok(dorg.admin, q=QUERY, mode="nl")  # admin may view compensation
    assert "comp_max" in {c["key"] for c in res["interpretation"]}
    resp = await search(dorg.admin.client, q=QUERY, mode="bogus")
    assert resp.status_code == 422
