"""Base semantic sources filter correctly on seed data (Epic 36 story 36.2)."""

from datetime import date
from typing import Any

import pytest

from app.core.database import tenant_session
from app.core.permissions import Scope
from app.core.semantic import SemanticError, compile, count, execute, ids, validate
from tests.core.semantic.conftest import World, sctx

SRC = "candidates.candidate"


async def _ids(world: World, filters: Any, **perms: Any) -> set[str]:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(**perms), world.tenant_a)
        found = set(await ids(validate({"source": SRC, "filters": filters}), ctx))
    names = {v: k for k, v in world.c.items()}
    return {names[f] for f in found}


def c(field: str, op: str, value: Any = None) -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


def _skill(world: World, min_p: int | None = None, *, other: bool = False) -> dict[str, Any]:
    sid = world.other_skill_id if other else world.skill_id
    return {"ref": {"kind": "tenant", "id": str(sid)}, "min_proficiency": min_p}


@pytest.mark.parametrize(
    ("make", "expect"),
    [
        (lambda w: c("full_name", "contains", "asha"), {"c1"}),
        (lambda w: c("full_name", "contains", "_100%"), {"c3"}),
        (lambda w: c("full_name", "contains", "%"), {"c3"}),
        (lambda w: c("full_name", "starts_with", "B"), {"c2"}),
        (lambda w: c("full_name", "contains", "x'; DROP TABLE candidates;--"), set()),
        (lambda w: c("current_title", "in", ["QA Lead", "Data Engineer"]), {"c1", "c2"}),
        (lambda w: c("current_employer", "eq", "Infosys"), {"c1"}),
        (lambda w: c("experience_years", "gte", 5), {"c1"}),
        (lambda w: c("experience_years", "between", [1, 3]), {"c2"}),
        (lambda w: c("experience_years", "is_null", True), {"c3", "c4"}),
        (lambda w: c("city", "eq", "Chennai"), {"c1", "c3"}),
        (lambda w: c("state", "in", ["Karnataka"]), {"c2"}),
        (lambda w: c("country", "is_null", True), {"c4"}),
        (lambda w: c("notice_period_days", "lt", 60), {"c1"}),
        (lambda w: c("availability_date", "within_days", {"days": 30, "direction": "future"}), {"c1"}),
        (lambda w: c("availability_date", "within_days", 30), set()),
        (lambda w: c("availability_date", "gt", date(2000, 1, 1).isoformat()), {"c1"}),
        (lambda w: c("expected_comp_inr", "lt", 1500000), {"c1"}),
        (lambda w: c("expected_comp_inr", "is_null", True), {"c3", "c4"}),
        (lambda w: c("work_authorization", "contains", "citizen"), {"c1"}),
        (lambda w: c("willing_to_relocate", "eq", True), {"c1"}),
        (lambda w: c("tags.name", "eq", w.tag_name), {"c1"}),
        (lambda w: c("source", "in", ["referral", "job_board"]), {"c2", "c3"}),
        (lambda w: c("source", "not_in", ["manual"]), {"c2", "c3"}),
        (lambda w: c("owner_id", "eq", str(w.users["u2"])), {"c2"}),
        (lambda w: c("id", "in", [str(w.c["c4"]), str(w.c["c5"])]), {"c4"}),
        (lambda w: c("created_at", "within_days", 1), {"c1", "c2", "c3", "c4"}),
        (lambda w: c("last_updated_at", "within_days", 1), {"c1", "c2", "c3", "c4"}),
        (lambda w: c("is_archived", "eq", True), {"c2"}),
        (lambda w: c("skills", "skill_match", _skill(w)), {"c1", "c2", "c3"}),
        (lambda w: c("skills", "skill_match", _skill(w, 3)), {"c1"}),
        (lambda w: c("skills", "skill_match", _skill(w, 2)), {"c1", "c3"}),
        (lambda w: c("skills", "skill_match", _skill(w, other=True)), {"c4"}),
        (lambda w: c("stage_history.stage_key", "eq", "client_interview"), set()),
        (lambda w: c("placements.status", "in", ["active"]), set()),
        (lambda w: c("placements.end_date", "within_days", 30), set()),
        (lambda w: c("bench.status", "eq", "on_bench"), set()),
        (
            lambda w: {
                "and": [
                    c("stage_history.stage_key", "in", ["x"]),
                    c("stage_history.entered_at", "within_days", 365),
                ]
            },
            set(),
        ),
        (lambda w: {"or": [c("city", "eq", "Pune"), c("is_archived", "eq", True)]}, {"c2", "c4"}),
        (lambda w: {"and": [c("city", "eq", "Chennai"), c("skills", "skill_match", _skill(w, 3))]}, {"c1"}),
    ],
)
async def test_candidate_fields_filter(world: World, make: Any, expect: set[str]) -> None:
    assert await _ids(world, make(world)) == expect


async def test_consent_display_only(world: World) -> None:
    with pytest.raises(SemanticError) as info:
        validate({"source": SRC, "filters": c("consent_marketing", "eq", "granted")})
    assert info.value.code == "semantic_operator_not_allowed"
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        cq = await compile(validate({"source": SRC, "fields": ["consent_marketing", "full_name"]}), ctx)
        rows = (await execute(cq, ctx)).rows
    by_id = {r["subject_id"]: r["consent_marketing"] for r in rows}
    assert by_id[world.c["c1"]] == "granted"
    assert by_id[world.c["c2"]] == "withdrawn"
    assert by_id[world.c["c3"]] == "none"


async def test_compensation_masked_for_recruiter(world: World) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(candidate__read=Scope.TENANT), world.tenant_a)
        cq = await compile(validate({"source": SRC, "fields": ["expected_comp_inr"]}), ctx)
        result = await execute(cq, ctx)
    assert result.masked_fields == ["expected_comp_inr"]
    assert all(r["expected_comp_inr"] is None for r in result.rows)
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        cq = await compile(validate({"source": SRC, "fields": ["expected_comp_inr"]}), ctx)
        rows = (await execute(cq, ctx)).rows
    assert any(r["expected_comp_inr"] is not None for r in rows)


async def test_group_by_and_measures_execute(world: World) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        defn = validate(
            {
                "source": SRC,
                "group_by": [{"field": "city"}, {"field": "created_at", "bucket": "fy"}],
                "measures": [
                    {"key": "n", "agg": "count"},
                    {"key": "exp", "agg": "avg", "field": "experience_years"},
                    {"key": "r", "agg": "ratio", "of": ["exp", "n"]},
                    {"key": "share", "agg": "pct_of_total"},
                ],
                "sort": [{"key": "n", "desc": True}],
            }
        )
        result = await execute(await compile(defn, ctx), ctx)
        for bucket in ("day", "week", "month", "quarter"):
            d = validate({"source": SRC, "group_by": [{"field": "availability_date", "bucket": bucket}]})
            assert (await execute(await compile(d, ctx), ctx)).rows
    top = result.rows[0]
    assert top["city"] == "Chennai" and top["n"] == 2
    assert isinstance(top["created_at"], str) and len(top["created_at"]) == 5  # "26-27"
    assert sum(r["share"] for r in result.rows) == pytest.approx(100)


@pytest.mark.parametrize(
    "filters",
    [
        None,
        c("status", "in", ["active", "ended"]),
        c("effective_end_date", "within_days", 30),
        c("planned_start_date", "between", ["2026-01-01", "2026-12-31"]),
        c("role_title", "contains", "dev"),
        c("compliance_blocked", "eq", False),
        c("candidate.full_name", "contains", "asha"),
        c("candidate.notice_period_days", "lte", 30),
        c("skills", "skill_match", {"ref": {"kind": "global", "id": "00000000-0000-0000-0000-000000000001"}}),
    ],
)
async def test_placements_worker_executes(world: World, filters: Any) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        defn = validate({"source": "placements.worker", "filters": filters})
        assert await count(defn, ctx) == 0
        cq = await compile(
            validate(
                {"source": "placements.worker", "filters": filters, "fields": ["status", "candidate.city"]}
            ),
            ctx,
        )
        assert (await execute(cq, ctx)).rows == []
