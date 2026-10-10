"""Semantic compiler: operators, EXISTS grouping, masking, permissions, scope, bound parameters only.

Pure compilation (no database): statements are compiled with the PostgreSQL dialect and inspected.
"""

import re
import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.permissions import Principal, Scope
from app.core.semantic import CompiledQuery, SemanticContext, SemanticError, compile, validate
from app.core.semantic.operators import register_skill_expander
from app.core.skill_refs import ExpandedSkill, SkillKey

SRC = "candidates.candidate"
T = Scope.TENANT
TENANT = uuid.uuid4()
ME = uuid.uuid4()
TEAM = uuid.uuid4()
BU = uuid.uuid4()
FULL = {"candidate:read": T, "compensation:view": T, "placement:read": T, "bench:read": T}


def principal(perms: dict[str, Scope] | None = None, kind: str = "user") -> Principal:
    return Principal(
        type=kind,  # type: ignore[arg-type]
        id=ME,
        tenant_id=TENANT,
        permissions=FULL if perms is None else perms,
        team_ids=frozenset({TEAM}),
        business_unit_id=BU,
    )


def ctx(p: Principal | None = None) -> SemanticContext:
    return SemanticContext(RequestContext(tenant_id=TENANT, principal=p or principal()), None, "segment")  # type: ignore[arg-type]


async def build(defn: dict[str, Any], p: Principal | None = None) -> CompiledQuery:
    return await compile(validate({"source": SRC, **defn}), ctx(p))


def sql(stmt: Any) -> tuple[str, dict[str, Any]]:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def cond(field: str, op: str, value: Any = None) -> dict[str, Any]:
    return {"filters": {"field": field, "op": op, "value": value}}


SKILL = {"ref": {"kind": "tenant", "id": str(uuid.uuid4())}, "min_proficiency": 3}
CASES = [
    ("id", "eq", str(uuid.uuid4())),
    ("id", "in", [str(uuid.uuid4())]),
    ("id", "not_in", [str(uuid.uuid4())]),
    ("human_id", "starts_with", "CAN-"),
    ("full_name", "contains", "asha"),
    ("full_name", "eq", "Asha"),
    ("full_name", "in", ["Asha", "Bala"]),
    ("experience_years", "gte", 3.5),
    ("experience_years", "lt", "10"),
    ("experience_years", "between", [1, 5]),
    ("experience_years", "is_null", True),
    ("city", "eq", "Chennai"),
    ("city", "is_null", False),
    ("notice_period_days", "lte", 30),
    ("notice_period_days", "gt", 0),
    ("availability_date", "within_days", {"days": 30, "direction": "future"}),
    ("availability_date", "between", ["2026-01-01", "2026-12-31"]),
    ("expected_comp_inr", "lt", 1500000),
    ("work_authorization", "contains", "citizen"),
    ("willing_to_relocate", "eq", True),
    ("tags.name", "in", ["java"]),
    ("source", "not_in", ["referral"]),
    ("owner_id", "eq", str(uuid.uuid4())),
    ("created_at", "within_days", 7),
    ("last_updated_at", "gte", "2026-01-01T00:00:00+05:30"),
    ("is_archived", "eq", False),
    ("stage_history.stage_key", "eq", "client_interview"),
    ("placements.status", "in", ["active"]),
    ("placements.end_date", "within_days", 30),
    ("placements.end_date", "is_null", True),
    ("bench.status", "eq", "on_bench"),
    ("skills", "skill_match", SKILL),
]


@pytest.mark.parametrize(("field", "op", "value"), CASES)
async def test_every_operator_compiles_with_bound_values(field: str, op: str, value: Any) -> None:
    cq = await build(cond(field, op, value))
    text, params = sql(cq.stmt)
    scalars = value if isinstance(value, list) else [value]
    for v in scalars:
        if isinstance(v, str) and len(v) > 2 and not v.startswith("20"):
            assert v not in text, f"literal {v!r} leaked into SQL"
    assert params  # tenant id at least


async def test_injection_value_is_bound() -> None:
    evil = "x'; DROP TABLE candidates;--"
    cq = await build(cond("full_name", "contains", evil))
    text, params = sql(cq.stmt)
    assert "DROP TABLE" not in text
    assert any(isinstance(v, str) and "DROP TABLE" in v for v in params.values())


async def test_like_wildcards_escaped() -> None:
    cq = await build(cond("full_name", "contains", "50%_off\\"))
    _, params = sql(cq.stmt)
    assert "%50\\%\\_off\\\\%" in params.values()


async def test_many_join_siblings_share_one_exists() -> None:
    defn = {
        "filters": {
            "and": [
                {"field": "stage_history.stage_key", "op": "in", "value": ["client_interview"]},
                {"field": "stage_history.entered_at", "op": "within_days", "value": 365},
                {"field": "city", "op": "eq", "value": "Chennai"},
            ]
        }
    }
    text, _ = sql((await build(defn)).stmt)
    assert text.count("EXISTS") == 1
    exists_part = text[text.index("EXISTS") :]
    assert "stage_key" in exists_part and "entered_at" in exists_part


async def test_many_join_in_or_gets_separate_exists() -> None:
    defn = {
        "filters": {
            "or": [
                {"field": "stage_history.stage_key", "op": "eq", "value": "a"},
                {"field": "stage_history.entered_at", "op": "within_days", "value": 30},
            ]
        }
    }
    text, _ = sql((await build(defn)).stmt)
    assert text.count("EXISTS") == 2


async def test_compensation_masked_in_fields_and_forbidden_in_filters() -> None:
    recruiter = principal({"candidate:read": T})
    cq = await build({"fields": ["full_name", "expected_comp_inr"]}, recruiter)
    assert cq.masked_fields == ["expected_comp_inr"]
    assert [c.masked for c in cq.columns] == [False, True]
    text, _ = sql(cq.stmt)
    assert "expected_comp_inr" not in text
    with pytest.raises(SemanticError) as info:
        await build(cond("expected_comp_inr", "lt", 1500000), recruiter)
    assert info.value.code == "semantic_field_forbidden"
    with pytest.raises(SemanticError):
        await build({"measures": [{"key": "avg", "agg": "avg", "field": "expected_comp_inr"}]}, recruiter)
    with pytest.raises(SemanticError) as info:
        await build({"fields": ["expected_comp_inr"], "sort": [{"key": "expected_comp_inr"}]}, recruiter)
    assert info.value.code == "semantic_field_forbidden"
    # With compensation:view it is selected, unmasked.
    cq = await build({"fields": ["expected_comp_inr"]})
    assert cq.masked_fields == []
    assert "expected_comp_inr" in sql(cq.stmt)[0]


async def test_join_permission_forbidden() -> None:
    no_placements = principal({"candidate:read": T})
    with pytest.raises(SemanticError) as info:
        await build(cond("placements.status", "eq", "active"), no_placements)
    assert info.value.code == "semantic_field_forbidden"
    with pytest.raises(SemanticError):
        await build(cond("bench.status", "eq", "on_bench"), no_placements)


async def test_source_permission_and_external_principals() -> None:
    with pytest.raises(Forbidden):
        await build({}, principal({"report:read": T}))
    for kind in ("client_user", "vendor_user", "candidate"):
        with pytest.raises(Forbidden):
            await build({}, principal(kind=kind))
    with pytest.raises(Forbidden):
        await compile(
            validate({"source": SRC}), SemanticContext(RequestContext(tenant_id=TENANT), None, "report")
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("scope", "expect"),
    [
        (Scope.OWN, ["owner_id = %(owner_id_1)s"]),
        (Scope.TEAM, ["owner_id = %(owner_id_1)s", "team_members"]),
        (Scope.BU, ["team_members", "users.business_unit_id"]),
        (Scope.TENANT, []),
    ],
)
async def test_scope_condition(scope: Scope, expect: list[str]) -> None:
    text, params = sql((await build({}, principal({"candidate:read": scope}))).stmt)
    for fragment in expect:
        assert fragment in text
    if scope is Scope.TENANT:
        assert "owner_id" not in text.split("WHERE", 1)[1]
    else:
        assert ME in params.values()


async def test_placements_scope_ors_owner_columns() -> None:
    p = principal({"placement:read": Scope.OWN})
    cq = await compile(validate({"source": "placements.worker"}), ctx(p))
    text, _ = sql(cq.stmt)
    assert re.search(r"recruiter_user_id = .* OR .*account_manager_user_id = ", text)


async def test_system_context_skips_scope_and_masking() -> None:
    sys_ctx = SemanticContext.for_system(TENANT, None, "refresh")  # type: ignore[arg-type]
    cq = await compile(validate({"source": SRC, "fields": ["expected_comp_inr"]}), sys_ctx)
    assert cq.masked_fields == []
    text, _ = sql(cq.stmt)
    assert "owner_id" not in text
    with pytest.raises(ValueError, match="purpose"):
        SemanticContext.for_system(TENANT, None, "anything")  # type: ignore[arg-type]


async def test_base_filter_and_tenant_filter_always_applied() -> None:
    text, params = sql((await build({})).stmt)
    assert "merged_into_id IS NULL" in text and "erased_at IS NULL" in text
    assert TENANT in params.values()


async def test_group_by_buckets_and_measures() -> None:
    defn = {
        "group_by": [{"field": "created_at", "bucket": "fy"}, {"field": "city"}],
        "measures": [
            {"key": "n", "agg": "count"},
            {"key": "exp", "agg": "avg", "field": "experience_years"},
            {"key": "r", "agg": "ratio", "of": ["exp", "n"]},
            {"key": "share", "agg": "pct_of_total"},
        ],
        "sort": [{"key": "n", "desc": True}],
    }
    cq = await build(defn)
    text, _ = sql(cq.stmt)
    assert "GROUP BY 1, 2" in text
    assert "interval '3 months'" in text
    assert "OVER ()" in text and "nullif" in text
    assert [c.key for c in cq.columns] == ["created_at", "city", "n", "exp", "r", "share"]
    assert cq.columns[0].type == "string"


async def test_skill_expander_used_only_when_expand() -> None:
    calls: list[Any] = []
    child = SkillKey("tenant", uuid.uuid4())

    async def expander(_ctx: Any, _db: Any, refs: Any, depth: int) -> list[ExpandedSkill]:
        calls.append((list(refs), depth))
        return [ExpandedSkill(r, r, __import__("decimal").Decimal("1"), 0, None) for r in refs] + [
            ExpandedSkill(child, refs[0], __import__("decimal").Decimal("0.8"), 1, refs[0])
        ]

    register_skill_expander(expander)
    try:
        cq = await build(cond("skills", "skill_match", {**SKILL, "expand": True}))
        _, params = sql(cq.stmt)
        assert calls and calls[0][1] == 1
        assert any(isinstance(v, list) and child.id in v for v in params.values())
        calls.clear()
        await build(cond("skills", "skill_match", SKILL))
        assert calls == []
    finally:
        register_skill_expander(None)
