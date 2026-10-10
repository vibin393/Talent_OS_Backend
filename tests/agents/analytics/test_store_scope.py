# ruff: noqa: E501, D401
"""Epic 64: tools under Effective Rights (scope, masking, cross-tenant), the answer store (owner only, viewer re-preview,
needs_repair, purge, saved link), the ``/analytics/answers`` API and the erasure handler."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update

from app.core.agentic import get_tool
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.permissions import Principal
from app.models.analytics_answers import AnalyticsAnswer
from app.models.candidates import Candidate
from app.schemas.analytics_answers import CatalogueIn, ReportGetIn, RunQuestionIn
from app.schemas.report_builder import VizSpec
from app.services import analytics_answers_service as svc
from app.services.agent_runtime.principals import intersect
from app.services.agents.analytics.definition import CEILING
from app.services.analytics_answers_erasure import REDACTED_QUESTION, REDACTED_VALUE, AnalyticsAnswersHandler
from app.services.privacy.registry import ErasureContext
from app.services.reporting.builder.execute import run_definition
from tests.agents.analytics.helpers import SRC, user_ctx
from tests.reporting.builder.helpers import create_report, grant, revoke, seed_placements
from tests.reporting.helpers import API, ROrg, h

BASE = f"{API}/analytics/answers"
BY_CLIENT = {
    "source": SRC,
    "group_by": [{"field": "client.name"}],
    "measures": [{"key": "n", "agg": "count"}, {"key": "bill", "agg": "sum", "field": "bill_rate"}],
    "sort": [{"key": "client.name"}],
}


async def agent_tctx(org: ROrg, user_id: uuid.UUID, db: Any) -> SimpleNamespace:
    """The tool context the runtime builds: an ``agent`` principal with Effective Rights = owner ∩ ceiling."""
    owner = (await user_ctx(org.tenant_id, user_id)).principal
    assert isinstance(owner, Principal)
    principal = Principal(
        type="agent",
        id=uuid.uuid4(),
        tenant_id=org.tenant_id,
        permissions=intersect(owner.permissions, CEILING),
        team_ids=owner.team_ids,
        business_unit_id=owner.business_unit_id,
    )
    request = RequestContext(
        tenant_id=org.tenant_id,
        principal=principal,
        actor_kind="agent",
        actor_id=principal.id,
        on_behalf_of=user_id,
    )
    return SimpleNamespace(request=request, db=db, owner_user_id=user_id, run_id=None, agent_key="analytics")


async def tool(org: ROrg, user_id: uuid.UUID, key: str, args: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        tctx = await agent_tctx(org, user_id, db)
        return await get_tool(key).handler(tctx, args)


def ask(defn: dict[str, Any], viz: dict[str, Any] | None = None, **kw: Any) -> RunQuestionIn:
    return RunQuestionIn.model_validate(
        {
            "definition": defn,
            "viz": viz or {"type": "bar", "x": "client.name", "value": "n"},
            "question": "Placements by client",
            **kw,
        }
    )


# ================================================================ tools / scope
async def test_scope_masking_and_parity_with_the_builder(rorg: ROrg) -> None:
    await seed_placements(rorg)
    rec = rorg.recruiter
    out = (await tool(rorg, rec.user_id, "semantic.run", ask(BY_CLIENT))).data
    # Same numbers as the P3 builder run for that user (viewer-scoped): recruiter sees p1 (Acme) and p3 (Globex).
    ctx = await user_ctx(rorg.tenant_id, rec.user_id)
    async with tenant_session(rorg.tenant_id) as db:
        p3 = await run_definition(
            ctx,
            db,
            definition=BY_CLIENT,
            viz=VizSpec(type="bar", x="client.name", value="n"),
            mode="preview",
            record=False,
        )
    assert [r[1] for r in out.rows] == [r["n"] for r in p3.json_rows()] == [1, 1]
    assert set(out.masked_fields) == {"bill", "bill_rate"} and [c.key for c in out.columns] == [
        "client.name",
        "n",
    ]
    assert out.total == 2 and "Sum of Bill rate" in out.explanation
    # The admin sees all four with the bill sum.
    full = (await tool(rorg, rorg.admin.user_id, "semantic.run", ask(BY_CLIENT))).data
    assert (
        sum(r[1] for r in full.rows) == 4
        and full.masked_fields == []
        and [c.key for c in full.columns][-1] == "bill"
    )


async def test_out_of_scope_aggregate_never_leaks(rorg: ROrg, other_rorg: ROrg) -> None:
    seeded = await seed_placements(rorg)
    kpi = {"type": "kpi", "value": "n"}
    rb = rorg.recruiter_b  # p2, p4 only
    for target in (seeded.p["p1"], uuid.uuid4()):  # invisible vs non-existent: same answer
        defn = {
            "source": SRC,
            "filters": {"field": "id", "op": "eq", "value": str(target)},
            "measures": [{"key": "n", "agg": "count"}],
        }
        out = (await tool(rorg, rb.user_id, "semantic.run", ask(defn, kpi))).data
        assert out.rows == [[0]]
    # Another tenant sees nothing of tenant A.
    out = (
        await tool(
            other_rorg,
            other_rorg.admin.user_id,
            "semantic.run",
            ask({"source": SRC, "measures": [{"key": "n", "agg": "count"}]}, kpi),
        )
    ).data
    assert out.rows == [[0]]


async def test_catalogue_masked_entries_and_validate(rorg: ROrg) -> None:
    rec = rorg.recruiter
    listing = (await tool(rorg, rec.user_id, "semantic.catalogue", CatalogueIn())).data
    assert SRC in {s.key for s in listing.sources}
    src = (await tool(rorg, rec.user_id, "semantic.catalogue", CatalogueIn(source_key=SRC))).data
    bill = next(f for f in src.fields if f.key == "bill_rate")
    assert bill.masked and bill.ops == [] and bill.aggs == [] and not bill.groupable
    assert "client" in src.joins
    from app.schemas.analytics_answers import ValidateIn

    bad = ValidateIn.model_validate(
        {
            "definition": {
                "source": SRC,
                "filters": {"field": "bill_rate", "op": "gt", "value": 1},
                "group_by": [{"field": "client.name"}],
            },
            "viz": {"type": "bar", "x": "client.name"},
        }
    )
    out = (await tool(rorg, rec.user_id, "semantic.validate", bad)).data
    assert not out.valid and (out.errors[0].code, out.errors[0].path) == (
        "semantic_field_forbidden",
        "filters.field",
    )
    viz_bad = ValidateIn.model_validate(
        {"definition": {"source": SRC, "group_by": [{"field": "client.name"}]}, "viz": {"type": "kpi"}}
    )
    out = (await tool(rorg, rec.user_id, "semantic.validate", viz_bad)).data
    assert out.errors[0].code == "report_viz_invalid"


async def test_reports_tools_visibility(rorg: ROrg, other_rorg: ROrg) -> None:
    await seed_placements(rorg)
    report = await create_report(rorg.admin)
    from app.schemas.analytics_answers import ReportsListIn

    rows = (await tool(rorg, rorg.admin.user_id, "reports.list", ReportsListIn(source_key=SRC))).data.rows
    assert [r.name for r in rows] == ["Placements by client"]
    got = (await tool(rorg, rorg.admin.user_id, "reports.get", ReportGetIn(report_id=report["id"]))).data
    assert got.definition["source"] == SRC
    # Private report of another user / another tenant -> not found.
    for org, uid in ((rorg, rorg.recruiter.user_id), (other_rorg, other_rorg.admin.user_id)):
        try:
            await tool(org, uid, "reports.get", ReportGetIn(report_id=report["id"]))
        except NotFound:
            continue
        raise AssertionError("expected 404")
    assert (await tool(rorg, rorg.recruiter.user_id, "reports.list", ReportsListIn())).data.rows == []


# ================================================================ store + API
async def _answer(
    org: ROrg,
    user_id: uuid.UUID,
    defn: dict[str, Any] | None = None,
    viz: dict[str, Any] | None = None,
    **kw: Any,
) -> uuid.UUID:
    out = (await tool(org, user_id, "semantic.run", ask(defn or BY_CLIENT, viz, **kw))).data
    return out.answer_id


async def test_answer_store_owner_only_and_viewer_preview(rorg: ROrg, other_rorg: ROrg) -> None:
    await seed_placements(rorg)
    rec = rorg.recruiter
    await grant(rorg, "recruiter", compensation__view="tenant")
    aid = await _answer(rorg, rec.user_id, assumptions=["I took all time"])
    resp = await rec.client.get(f"{BASE}/{aid}", headers=h(rec))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["assumptions"] == ["I took all time"]
        and body["source_label"] == "Placements"
        and body["status"] == "ok"
    )
    assert "rows" not in body
    resp = await rec.client.post(f"{BASE}/{aid}/preview", headers=h(rec))
    assert resp.status_code == 200 and resp.json()["masked_fields"] == []
    assert [c["key"] for c in resp.json()["columns"]] == ["client.name", "n", "bill"]
    # Permission revoked since asking: the re-run (as the current viewer) masks the measure.
    await revoke(rorg, "recruiter", "compensation:view")
    resp = await rec.client.post(f"{BASE}/{aid}/preview", headers=h(rec))
    assert resp.status_code == 200 and set(resp.json()["masked_fields"]) == {"bill", "bill_rate"}
    # Owner only: another user of the tenant and another tenant get 404.
    for m in (rorg.manager, other_rorg.admin):
        for method, path in (("get", f"{BASE}/{aid}"), ("post", f"{BASE}/{aid}/preview")):
            assert (await getattr(m.client, method)(path, headers=h(m))).status_code == 404
        assert (
            await m.client.post(f"{BASE}/{aid}/saved", json={"report_id": str(uuid.uuid4())}, headers=h(m))
        ).status_code in (403, 404)
    listing = (await rec.client.get(BASE, headers=h(rec))).json()
    assert [i["id"] for i in listing["items"]] == [str(aid)]
    assert (await rorg.manager.client.get(BASE, headers=h(rorg.manager))).json()["items"] == []


async def test_needs_repair_and_timeout(rorg: ROrg, monkeypatch: Any) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    aid = await _answer(rorg, admin.user_id)
    async with tenant_session(rorg.tenant_id) as db:
        await db.execute(
            update(AnalyticsAnswer)
            .where(AnalyticsAnswer.id == aid)
            .values(definition={**BY_CLIENT, "group_by": [{"field": "ghost_field"}]})
        )
    resp = await admin.client.post(f"{BASE}/{aid}/preview", headers=h(admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "analytics_answer_needs_repair"
    assert (await admin.client.get(f"{BASE}/{aid}", headers=h(admin))).json()["status"] == "needs_repair"
    # A statement timeout is a 503 query_timeout.
    from app.core.semantic import SemanticTimeout
    from app.services.analytics_answers import service

    async def slow(*a: Any, **k: Any) -> Any:
        raise SemanticTimeout()

    aid2 = await _answer(rorg, admin.user_id)
    monkeypatch.setattr("app.core.semantic.executor.execute", slow)
    monkeypatch.setattr("app.services.reporting.builder.execute.execute", slow)
    resp = await admin.client.post(f"{BASE}/{aid2}/preview", headers=h(admin))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "query_timeout"
    assert service.MODEL_ROWS_MAX == 50


async def test_mark_saved_and_purge(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    aid = await _answer(rorg, admin.user_id)
    report = await create_report(admin, name="Saved from answer")
    resp = await admin.client.post(f"{BASE}/{aid}/saved", json={"report_id": report["id"]}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["saved_report_id"] == report["id"]
        and resp.json()["saved_report_name"] == "Saved from answer"
    )
    # A report the user cannot see -> 422 report_not_visible.
    private = await create_report(rorg.account, name="Private")
    resp = await admin.client.post(f"{BASE}/{aid}/saved", json={"report_id": private["id"]}, headers=h(admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "report_not_visible"
    # Purge: expired answers and answers of a conversation that no longer exists.
    expired = await _answer(rorg, admin.user_id)
    orphan = await _answer(rorg, admin.user_id)
    async with tenant_session(rorg.tenant_id) as db:
        await db.execute(
            update(AnalyticsAnswer)
            .where(AnalyticsAnswer.id == expired)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        await db.execute(
            update(AnalyticsAnswer).where(AnalyticsAnswer.id == orphan).values(conversation_id=uuid.uuid4())
        )
    async with tenant_session(rorg.tenant_id) as db:
        assert await svc.purge_expired(db) == 2
        left = set((await db.execute(select(AnalyticsAnswer.id))).scalars())
    assert left == {aid}


async def test_erasure_redacts_question_and_filter_values(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    async with tenant_session(rorg.tenant_id) as db:
        cand = (await db.execute(select(Candidate).limit(1))).scalar_one()
        name, cid = cand.full_name, cand.id
    defn = {
        "source": SRC,
        "filters": {"field": "candidate.full_name", "op": "eq", "value": name},
        "measures": [{"key": "n", "agg": "count"}],
    }
    aid = await _answer(rorg, admin.user_id, defn, {"type": "kpi", "value": "n"})
    other = await _answer(rorg, admin.user_id)
    async with tenant_session(rorg.tenant_id) as db:
        await db.execute(
            update(AnalyticsAnswer)
            .where(AnalyticsAnswer.id == aid)
            .values(question=f"How many placements for {name}?")
        )
    ctx = ErasureContext(tenant_id=rorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(rorg.tenant_id) as db:
        counts = await AnalyticsAnswersHandler().erase(db, ctx)
    assert counts == {"analytics_answers": 1}
    async with tenant_session(rorg.tenant_id) as db:
        a = await db.get(AnalyticsAnswer, aid)
        b = await db.get(AnalyticsAnswer, other)
        assert a is not None and b is not None
        assert a.question == REDACTED_QUESTION and a.assumptions == [] and a.status == "needs_repair"
        assert a.definition["filters"]["value"] == REDACTED_VALUE and a.definition["source"] == SRC
        assert b.question != REDACTED_QUESTION and b.status == "ok"
        assert await AnalyticsAnswersHandler().verify(db, ctx) == []
    assert dataclasses.is_dataclass(ctx)
