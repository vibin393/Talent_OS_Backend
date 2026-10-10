# ruff: noqa: E501, RUF005
"""Epic 64 pure / static checks: minimum-cell suppression (test source k=5), the agent definition and ceiling (pinned
against the registered semantic sources), the Copilot capability gating (C-4), tool registrations, no SQL anywhere
(grep + AST isolation), no expression inputs, and a hypothesis fuzz of tool inputs (422 / invalid, never a DB error)."""

import ast
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from app.core.agentic import copilot_capabilities, get_agent, get_tool
from app.core.agentic.copilot import enabled_capabilities
from app.core.hooks import autodiscover
from app.core.permissions import Principal, Scope
from app.core.semantic import Definition, SemanticError, sources, validate
from app.schemas import analytics_answers as schemas
from app.schemas.report_builder import VizSpec
from app.services.agent_runtime.state import allow_list
from app.services.analytics_answers import cells
from app.services.reporting.builder.validate import check_viz

autodiscover("app.services")
APP = Path(__file__).resolve().parents[3] / "app"
TOOL_KEYS = ("semantic.catalogue", "semantic.validate", "semantic.run", "reports.list", "reports.get")


# ================================================================ cells
@pytest.fixture
def k5() -> object:
    cells.register_min_cell_size("_test.source", 5)
    yield
    cells.MIN_CELL_SIZE.pop("_test.source", None)


def test_cells_suppress_small_groups(k5: object) -> None:
    defn = Definition.model_validate(
        {
            "source": "_test.source",
            "group_by": [{"field": "client"}],
            "measures": [{"key": "n", "agg": "count"}],
        }
    )
    rows = [{"client": "A", "n": 9}, {"client": "B", "n": 4}, {"client": "C", "n": 5}]
    out, hidden = cells.suppress("_test.source", defn, rows)
    assert hidden == 1 and out[1] == {"suppressed": True} and out[0]["client"] == "A" and out[2]["n"] == 5
    # implicit count column when there are no measures
    defn2 = Definition.model_validate({"source": "_test.source", "group_by": [{"field": "client"}]})
    assert cells.suppress("_test.source", defn2, [{"client": "A", "count": 2}])[1] == 1
    # no count measure -> fail closed
    defn3 = Definition.model_validate(
        {
            "source": "_test.source",
            "group_by": [{"field": "client"}],
            "measures": [{"key": "s", "agg": "sum", "field": "x"}],
        }
    )
    assert cells.suppress("_test.source", defn3, [{"client": "A", "s": 100}])[1] == 1
    # ungrouped and unlisted sources are untouched
    assert cells.suppress("placements.placement", defn, rows)[1] == 0
    assert (
        cells.min_cell_size("_test.source") == 5 and cells.MIN_CELL_SIZE.get("placements.placement") is None
    )


# ================================================================ definition / capability / tools
def test_agent_definition() -> None:
    spec = get_agent("analytics")
    assert not spec.placeholder and spec.owner_epic == "64" and spec.origin == "copilot"
    assert spec.tools == TOOL_KEYS and spec.prompt_id == "agent_analytics" and spec.fallback == "none"
    assert spec.limits.max_steps == 8 and spec.limits.max_tool_calls == 14 and spec.limits.max_wait_hours == 0
    assert spec.eval_set == "evals/analytics" and spec.eval_threshold == 0.9
    assert "identity:view" not in spec.permission_ceiling and "compensation:view" in spec.permission_ceiling
    source_perms = {s.scope.permission for s in sources().values()}
    assert source_perms <= set(spec.permission_ceiling), source_perms - set(spec.permission_ceiling)


def test_tools_registered_read_only() -> None:
    for key in TOOL_KEYS:
        t = get_tool(key)
        assert (t.risk, t.owner_epic, t.version, t.egress, t.permissions) == (
            "read",
            "64",
            1,
            False,
            ("report:read",),
        )
        assert t.input_model.model_config.get("extra") == "forbid"
        assert "No SQL" in t.description
    assert get_tool("semantic.run").untrusted_output == frozenset({"rows"})


def test_copilot_capability_gating() -> None:
    cap = copilot_capabilities()["analytics"]
    assert cap.tools == ("semantic.catalogue", "semantic.validate", "semantic.run", "reports.list")
    assert cap.prompt_section == "copilot_analytics.v1.md" and cap.agent_key == "analytics"
    on = {c.key for c in enabled_capabilities({"agent_analytics": True}, lambda p: p == "report:read")}
    assert "analytics" in on
    assert "analytics" not in {
        c.key for c in enabled_capabilities({"agent_analytics": False}, lambda p: True)
    }
    assert "analytics" not in {
        c.key for c in enabled_capabilities({"agent_analytics": True}, lambda p: False)
    }
    copilot = get_agent("copilot")
    import uuid

    reader = Principal(
        type="user", id=uuid.uuid4(), tenant_id=uuid.uuid4(), permissions={"report:read": Scope.TENANT}
    )
    assert "semantic.run" in allow_list(copilot, {"agent_analytics": True}, reader)
    assert "semantic.run" not in allow_list(copilot, {"agent_analytics": False}, reader)
    assert "reports.get" not in allow_list(copilot, {"agent_analytics": True}, reader)


# ================================================================ containment
PATTERNS = [
    re.compile(r"\btext\("),
    re.compile(r"\bliteral_column\("),
    re.compile(r"\.execute\(\s*[\"']"),
    re.compile(r"\bexec_driver_sql\("),
    re.compile(r"f[\"'][^\"']*\b(SELECT|INSERT|UPDATE|DELETE|WHERE|FROM)\b", re.IGNORECASE),
    re.compile(r"[\"']\s*(SELECT|INSERT INTO|UPDATE|DELETE FROM)\s", re.IGNORECASE),
]


def test_no_raw_sql() -> None:
    files = [
        *(APP / "services" / "analytics_answers").glob("*.py"),
        *(APP / "services" / "agents" / "analytics").glob("*.py"),
        *(APP / "services" / "agent_tools" / "reporting").glob("*.py"),
        APP / "services" / "analytics_answers_service.py",
        APP / "router" / "analytics_answers.py",
    ]
    assert len(files) >= 14
    hits = []
    for path in files:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            hits.extend(f"{path.name}:{no}: {line.strip()}" for p in PATTERNS if p.search(line))
    assert hits == []


def test_agent_package_isolation() -> None:
    for path in (APP / "services" / "agents" / "analytics").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not name.startswith(
                    (
                        "sqlalchemy",
                        "anthropic",
                        "app.models",
                        "app.core.database",
                        "app.services.agent_runtime.",
                    )
                ), (path.name, name)


def test_no_tool_input_accepts_sql_or_expressions() -> None:
    for key in TOOL_KEYS:
        model = get_tool(key).input_model
        stack: list[type[BaseModel]] = [model]
        seen: set[type[BaseModel]] = set()
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            assert m.model_config.get("extra") == "forbid", m
            for name, f in m.model_fields.items():
                assert name.lower() not in {"sql", "query_text", "expression", "formula", "code"}, (m, name)
                ann = f.annotation
                for sub in getattr(ann, "__args__", ()) + (ann,):
                    if isinstance(sub, type) and issubclass(sub, BaseModel):
                        stack.append(sub)
    assert Definition.model_config.get("extra") == "forbid" and VizSpec.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        schemas.RunQuestionIn.model_validate(
            {"definition": {"source": "x"}, "question": "q", "sql": "SELECT 1"}
        )
    with pytest.raises(ValidationError):
        schemas.RunQuestionIn.model_validate(
            {"definition": {"source": "x", "expression": "1"}, "question": "q"}
        )


FIELDS = [
    "client.name",
    "planned_start_date",
    "status",
    "bill_rate",
    "ghost",
    "1;--",
    "'; DROP TABLE x; --",
    "",
]
JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=12) | st.sampled_from(FIELDS),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(
        st.sampled_from(
            [
                "source",
                "field",
                "op",
                "value",
                "and",
                "or",
                "group_by",
                "measures",
                "fields",
                "sort",
                "sql",
                "key",
                "agg",
            ]
        ),
        children,
        max_size=4,
    ),
    max_leaves=12,
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    raw=JSON,
    viz=st.sampled_from(
        [
            {"type": "table"},
            {"type": "bar", "x": "client.name"},
            {"type": "kpi"},
            {"type": "pie", "x": "ghost"},
        ]
    ),
)
def test_fuzz_inputs_never_reach_the_database(raw: object, viz: dict[str, str]) -> None:
    """Random JSON -> pydantic ValidationError or a 422 semantic / viz error; anything that validates names only
    registered keys (compile is exercised end to end in test_scripts / test_scope)."""
    try:
        data = schemas.RunQuestionIn.model_validate(
            {
                "definition": {"source": "placements.placement", **raw} if isinstance(raw, dict) else raw,
                "viz": viz,
                "question": "q",
            }
        )
    except ValidationError:
        return
    try:
        defn = validate(data.definition.model_dump(mode="json", by_alias=True, exclude_none=True))
        check_viz(data.viz, defn)
    except (SemanticError, Exception) as exc:
        from app.core.errors import ValidationFailed

        assert isinstance(exc, ValidationFailed), repr(exc)
