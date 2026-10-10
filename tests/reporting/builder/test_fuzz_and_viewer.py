# ruff: noqa: E501
"""47.2 / 47.3: hypothesis fuzz of report definitions + viz (422, never a DB error) and the pure viewer adaptation."""

import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import DomainError
from app.core.permissions import Principal, Scope
from app.core.semantic import SemanticContext, compile, execute, validate
from app.schemas.report_builder import VizSpec
from app.schemas.semantic import FieldCatalogOut, SourceCatalogOut
from app.services.reporting.builder import hooks as builder_hooks
from app.services.reporting.builder.validate import check_viz
from app.services.reporting.builder.viewer import viewer_definition
from tests.factories import create_tenant_row

builder_hooks.register()

SRC = "placements.placement"
FIELDS = ["client.name", "planned_start_date", "status", "bill_rate", "pay_rate", "role_title", "human_id", "demand.title", "candidate.full_name", "ghost", "client.ghost", "1;--", ""]
AGGS = ["count", "count_distinct", "sum", "avg", "min", "max", "ratio", "pct_of_total", "median", ""]
BUCKETS = [None, "day", "week", "month", "quarter", "fy", "year"]
VIZ = st.fixed_dictionaries(
    {"type": st.sampled_from(["table", "pivot", "bar", "line", "pie", "kpi"])},
    optional={"x": st.sampled_from(FIELDS), "series": st.sampled_from(FIELDS), "value": st.sampled_from(["m0", "m1", "count", *FIELDS])},
)
MEASURES = st.lists(
    st.fixed_dictionaries(
        {"key": st.sampled_from(["m0", "m1", "m2", "Bad Key"]), "agg": st.sampled_from(AGGS)},
        optional={"field": st.sampled_from(FIELDS), "of": st.lists(st.sampled_from(["m0", "m1", "zz"]), min_size=2, max_size=2)},
    ),
    max_size=4,
)
DEFINITIONS = st.fixed_dictionaries(
    {"source": st.sampled_from([SRC, SRC, "nope.nope"])},
    optional={
        "group_by": st.lists(st.fixed_dictionaries({"field": st.sampled_from(FIELDS)}, optional={"bucket": st.sampled_from(BUCKETS)}), max_size=4),
        "measures": MEASURES,
        "fields": st.lists(st.sampled_from(FIELDS), max_size=4),
        "sort": st.lists(st.fixed_dictionaries({"key": st.sampled_from(["m0", "m1", *FIELDS])}, optional={"desc": st.booleans()}), max_size=3),
        "expression": st.sampled_from(["bill_rate*2", "1"]),
    },
)

_state: dict[str, Any] = {}


async def _tenant() -> uuid.UUID:
    if "tenant" not in _state:
        _state["tenant"] = await create_tenant_row("Fuzz reports")
    return _state["tenant"]  # type: ignore[no-any-return]


def _principal(tenant_id: uuid.UUID, *, comp: bool) -> Principal:
    perms = {"placement:read": Scope.TENANT}
    if comp:
        perms["compensation:view"] = Scope.TENANT
    return Principal(type="user", id=uuid.uuid4(), tenant_id=tenant_id, permissions=perms)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(defn=DEFINITIONS, viz=VIZ, comp=st.booleans())
async def test_definitions_and_viz_fuzz(defn: dict[str, Any], viz: dict[str, Any], comp: bool) -> None:
    tenant_id = await _tenant()
    principal = _principal(tenant_id, comp=comp)
    try:
        parsed = validate(defn, principal=principal)
        check_viz(VizSpec.model_validate(viz), parsed)
    except DomainError as exc:  # 422 semantic_* / report_viz_invalid -- never a DB error
        assert exc.status_code in {403, 422}, exc
        return
    except ValueError:  # pydantic refusal of the viz shape (FastAPI turns it into 422)
        return
    async with tenant_session(tenant_id) as db:
        ctx = SemanticContext(RequestContext(tenant_id=tenant_id, principal=principal), db, "report")
        try:
            cq = await compile(parsed, ctx)
        except DomainError as exc:
            assert exc.status_code in {403, 422}, exc
            return
        result = await execute(cq, ctx, limit=10)
        assert result.total is not None and len(result.rows) <= 10


def _catalogue(masked: set[str]) -> SourceCatalogOut:
    def f(key: str) -> FieldCatalogOut:
        return FieldCatalogOut(key=key, label=key, type="decimal", pii_class="compensation" if key in masked else "none", ops=[], aggs=[], groupable=False, date_buckets=False, displayable=True, masked=key in masked)

    return SourceCatalogOut(key=SRC, label="P", fields=[f(k) for k in ("bill_rate", "pay_rate", "client.name")])


def test_viewer_removes_masked_measures_and_dependent_ratios() -> None:
    defn = validate({
        "source": SRC,
        "group_by": [{"field": "client.name"}],
        "measures": [
            {"key": "n", "agg": "count"},
            {"key": "bill", "agg": "sum", "field": "bill_rate"},
            {"key": "pay", "agg": "sum", "field": "pay_rate"},
            {"key": "ratio", "agg": "ratio", "of": ["bill", "pay"]},
            {"key": "share", "agg": "pct_of_total", "field": "bill_rate"},
        ],
        "sort": [{"key": "bill", "desc": True}, {"key": "n"}],
    })
    viz = VizSpec(type="bar", x="client.name", value="bill")
    out = viewer_definition(defn, viz, _catalogue({"bill_rate", "pay_rate"}))
    assert [m.key for m in out.definition.measures] == ["n"]
    assert [s.key for s in out.definition.sort] == ["n"]
    assert set(out.removed_measures) == {"bill", "pay", "ratio", "share"}
    assert set(out.masked) == {"bill", "pay", "ratio", "share", "bill_rate", "pay_rate"}
    assert out.viz.value == "n"
    # Nothing masked -> unchanged; a KPI over masked fields only stays an aggregate.
    same = viewer_definition(defn, viz, _catalogue(set()))
    assert same.definition is defn and same.masked == ()
    kpi = validate({"source": SRC, "measures": [{"key": "bill", "agg": "sum", "field": "bill_rate"}]})
    out = viewer_definition(kpi, VizSpec(type="kpi", value="bill"), _catalogue({"bill_rate"}))
    assert [(m.key, m.agg) for m in out.definition.measures] == [("count", "count")]
    # Masked fields in group_by / filters are NOT removed (the foundation refuses them for this viewer).
    filtered = validate({"source": SRC, "filters": {"field": "bill_rate", "op": "gt", "value": 1}, "fields": ["human_id"]})
    assert viewer_definition(filtered, VizSpec(), _catalogue({"bill_rate"})).definition.filters is not None
