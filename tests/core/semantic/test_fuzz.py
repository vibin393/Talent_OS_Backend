"""Hypothesis fuzz of the semantic layer (Epic 36 story 36.2).

Random field / operator / value trees either compile to SQL whose only literals are our own constants (every
user value is a bound parameter) or are refused with a 422 ``semantic_*`` error -- never a database syntax
error and never a 500.
"""

import re
import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.dialects import postgresql

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal, Scope
from app.core.semantic import SemanticContext, SemanticError, compile, count, execute, get_source, validate
from app.core.semantic.types import OPS
from app.services.candidates import semantic_source as candidate_source
from app.services.placements import semantic_source as placement_source
from tests.core.semantic.conftest import World, sctx

candidate_source.register()
placement_source.register()

SOURCES = ["candidates.candidate", "placements.worker"]
FIELDS = [
    *sorted(set(get_source(SOURCES[0]).fields) | set(get_source(SOURCES[1]).fields)),
    "salary", "ghost.x", "tags.nope", "City", "1=1", "full_name; --", ""
]  # fmt: skip
OP_CHOICES = [*sorted(OPS), "regex", "", "EQ"]
ALLOWED_LITERALS = {"'day'", "'week'", "'month'", "'quarter'", "'year'", "'3 months'", "'none'", "'\\\\'"}

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=20),
    st.sampled_from(
        ["2026-01-01", "2026-13-40", "2026-05-01T10:00:00+05:30", str(uuid.uuid4()), "x'; DROP--"]
    ),
    st.sampled_from(["active", "granted", "manual", "on_bench", "Chennai", "%", "_", "\\"]),
)
values = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(
            st.sampled_from(["days", "direction", "ref", "kind", "id", "min_proficiency", "expand"]),
            inner,
            max_size=3,
        ),
    ),
    max_leaves=6,
)
skill_values = st.fixed_dictionaries(
    {
        "ref": st.fixed_dictionaries(
            {"kind": st.sampled_from(["global", "tenant"]), "id": st.uuids().map(str)}
        )
    },
    optional={"min_proficiency": st.one_of(st.none(), st.integers(0, 6)), "expand": st.booleans()},
)
conditions = st.fixed_dictionaries(
    {
        "field": st.sampled_from(FIELDS),
        "op": st.sampled_from(OP_CHOICES),
        "value": st.one_of(values, skill_values),
    }
)
nodes = st.recursive(
    conditions,
    lambda inner: st.one_of(
        st.fixed_dictionaries({"and": st.lists(inner, min_size=0, max_size=4)}),
        st.fixed_dictionaries({"or": st.lists(inner, min_size=0, max_size=4)}),
    ),
    max_leaves=10,
)
definitions = st.fixed_dictionaries(
    {"source": st.sampled_from([*SOURCES, "nope.src"])},
    optional={
        "filters": st.one_of(st.none(), nodes),
        "fields": st.lists(st.sampled_from(FIELDS), max_size=4),
        "group_by": st.lists(
            st.fixed_dictionaries(
                {"field": st.sampled_from(FIELDS)},
                optional={"bucket": st.sampled_from([None, "day", "month", "fy", "decade"])},
            ),
            max_size=2,
        ),
        "measures": st.lists(
            st.fixed_dictionaries(
                {
                    "key": st.sampled_from(["n", "m", "x"]),
                    "agg": st.sampled_from(["count", "sum", "avg", "ratio", "pct_of_total", "max"]),
                },
                optional={
                    "field": st.sampled_from(FIELDS),
                    "of": st.tuples(st.sampled_from(["n", "m"]), st.sampled_from(["n", "m"])),
                },
            ),
            max_size=3,
        ),
    },
)

PRINCIPAL = Principal(
    type="user",
    id=uuid.uuid4(),
    tenant_id=uuid.uuid4(),
    permissions={"candidate:read": Scope.TEAM, "placement:read": Scope.OWN},  # no compensation:view
    team_ids=frozenset({uuid.uuid4()}),
)


def _check_sql(stmt: Any) -> None:
    text = str(stmt.compile(dialect=postgresql.dialect()))
    literals = set(re.findall(r"'[^']*'", text))
    assert literals <= ALLOWED_LITERALS, literals - ALLOWED_LITERALS


async def _compile_or_422(defn: dict[str, Any], ctx: SemanticContext) -> Any:
    try:
        parsed = validate(defn, principal=PRINCIPAL)
        cq = await compile(parsed, ctx)
    except SemanticError as exc:
        assert exc.status_code == 422 and exc.code.startswith("semantic_")
        assert isinstance(exc.details.get("path"), str)
        return None
    _check_sql(cq.stmt)
    assert cq.ids_stmt is not None and cq.count_stmt is not None
    _check_sql(cq.ids_stmt)
    return parsed


@settings(max_examples=400, deadline=None)
@given(definitions)
async def test_fuzz_compiles_or_422(defn: dict[str, Any]) -> None:
    ctx = SemanticContext(RequestContext(tenant_id=PRINCIPAL.tenant_id, principal=PRINCIPAL), None, "segment")  # type: ignore[arg-type]
    await _compile_or_422(defn, ctx)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(defn=definitions)
async def test_fuzz_executes_without_db_errors(world: World, defn: dict[str, Any]) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, PRINCIPAL, world.tenant_a)
        parsed = await _compile_or_422(defn, ctx)
        if parsed is not None:
            result = await execute(await compile(parsed, ctx), ctx, limit=5)
            assert result.total is not None and len(result.rows) <= 5
            assert await count(parsed, ctx) >= 0
