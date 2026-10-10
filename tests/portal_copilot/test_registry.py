# ruff: noqa: E501
"""Epic 77 registry rules (no DB): the portal AgentSpecs fill the placeholders with portal tools only, ceilings carry
portal permission keys only, every portal tool has an exposure on its surface, ``validate_exposures`` refuses a
non-``portal_`` tool on a portal surface, outputs carry no FR-80 forbidden field names, and the
``p5-portal-tools-isolation`` contract exists."""

import configparser
from pathlib import Path

import pytest

from app.core.agentic import agents, get_tool, tools
from app.core.agentic.exposure import ToolExposure, expose_tool, exposures, unexpose_tool, validate_exposures
from app.core.field_visibility import is_forbidden
from app.core.hooks import autodiscover
from app.core.permissions import CATALOG

KINDS = ("client", "vendor", "candidate")
SCAN = {"client": "client_user", "vendor": "vendor_user", "candidate": "worker"}


@pytest.fixture(scope="module", autouse=True)
def _discovered() -> None:
    autodiscover("app.services")


@pytest.mark.parametrize("kind", KINDS)
def test_agent_spec_is_real_and_portal_only(kind: str) -> None:
    spec = agents()[f"copilot_{kind}"]
    assert not spec.placeholder and spec.origin == "portal" and spec.owner_epic == "77"
    assert spec.prompt_id == f"agent_copilot_{kind}" and spec.default_mode == "suggest"
    assert set(spec.tools) == set(exposures(f"portal_{kind}"))  # type: ignore[arg-type]
    assert all(t.startswith(f"portal_{kind}.") for t in spec.tools)
    assert not set(spec.permission_ceiling) & set(CATALOG), "a portal ceiling never holds an internal permission"
    assert "portal_copilot:use" in spec.permission_ceiling
    for key in spec.tools:
        tool = get_tool(key)
        assert set(tool.permissions) <= set(spec.permission_ceiling), key
        assert "portal_copilot:use" in tool.permissions, key
        assert not tool.mcp_exposed, key  # portal tools are never MCP tools


@pytest.mark.parametrize("kind", KINDS)
def test_output_models_have_no_forbidden_fields(kind: str) -> None:
    def walk(model: type, seen: set[type]) -> list[str]:
        if model in seen:
            return []
        seen.add(model)
        out = []
        for name, field in model.model_fields.items():  # type: ignore[attr-defined]
            if is_forbidden(SCAN[kind], name) and not (kind == "vendor" and name == "client_name"):
                out.append(name)
            ann = field.annotation
            for arg in getattr(ann, "__args__", ()) or (ann,):
                if isinstance(arg, type) and hasattr(arg, "model_fields"):
                    out += walk(arg, seen)
        return out

    for key, tool in tools().items():
        if key.startswith(f"portal_{kind}."):
            assert walk(tool.output_model, set()) == [], key


def test_validate_exposures_refuses_a_staff_tool_on_a_portal_surface() -> None:
    staff = next(k for k in sorted(tools()) if not k.startswith("portal_") and k not in exposures())
    expose_tool(ToolExposure(staff, frozenset({"portal_client"})))  # type: ignore[arg-type]
    try:
        problems = validate_exposures()
        assert any(staff in p and "portal_" in p for p in problems)
    finally:
        unexpose_tool(staff)
    assert not [p for p in validate_exposures() if "portal" in p]


def test_star_actions_are_never_portal_tools() -> None:
    star_words = ("approve", "reject", "accept", "decline", "withdraw", "submit", "consent", "upload", "sign")
    for key in tools():
        if key.startswith("portal_"):
            verb = key.split(".", 1)[1]
            assert not any(verb.startswith(w) for w in star_words), key


def test_isolation_contract_declared() -> None:
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).resolve().parents[2] / ".importlinter", encoding="utf-8")
    section = cfg["importlinter:contract:p5-portal-tools-isolation"]
    assert section["type"] == "forbidden"
    sources = section["source_modules"].split()
    assert "app.services.agent_tools.portal_client" in sources and "app.services.agents.copilot_candidate" in sources
    forbidden = set(section["forbidden_modules"].split())
    assert {"app.models", "app.services.candidates", "app.services.pipeline.feedback", "app.services.agent_tools.candidates"} <= forbidden
    assert "app.services.pipeline.portal" not in forbidden and "app.services.vendors.portal" not in forbidden
    human_only = cfg["importlinter:contract:p5-human-only"]["source_modules"].split()
    assert "app.services.portal_copilot" in human_only
