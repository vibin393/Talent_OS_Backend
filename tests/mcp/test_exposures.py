# ruff: noqa: E501
"""Epic 73 (spec 0.3, FR-377): the exposure set is valid, ``mcp_exposed`` agrees, ★ actions are never exposed,
high / egress tools are proposals only, and every exposed permission is unlockable by a registered MCP-capable scope."""

from app.core import oauth_scopes
from app.core.agentic.exposure import exposures, validate_exposures
from app.core.agentic.tools import tools
from app.core.agentic.types import FORBIDDEN_ACTIONS
from app.core.hooks import autodiscover
from app.core.permissions import NON_DELEGABLE, STAR_PERMISSIONS
from app.services.mcp_server import catalog
from app.services.workflow import catalogue as wf

EXPECTED = {
    "search.global": ("mcp:read", "core"), "tasks.list": ("mcp:read", "core"), "activities.list": ("mcp:read", "core"),
    "navigation.screen_for": ("mcp:read", "core"), "candidates.search": ("mcp:read", "recruiting"),
    "candidates.get": ("mcp:read", "recruiting"), "demands.list": ("mcp:read", "recruiting"), "demands.get": ("mcp:read", "recruiting"),
    "pipeline.list": ("mcp:read", "recruiting"), "matching.rank": ("mcp:read", "recruiting"), "interviews.list": ("mcp:read", "interviews"),
    "placements.list": ("mcp:read", "delivery"), "sla.at_risk": ("mcp:read", "delivery"), "predictions.get": ("mcp:read", "delivery"),
    "semantic.catalogue": ("mcp:read", "analytics"), "semantic.run": ("mcp:read", "analytics"), "reports.list": ("mcp:read", "analytics"),
    "knowledge.search": ("mcp:read", "knowledge"), "notes.add": ("mcp:draft", "core"), "tasks.create": ("mcp:draft", "core"),
    "demands.create_draft": ("mcp:draft", "recruiting"), "messaging.create_draft": ("mcp:draft", "outreach"),
    "messaging.send": ("mcp:propose", "outreach"), "pipeline.add_candidate": ("mcp:propose", "recruiting"),
    "demands.update": ("mcp:propose", "recruiting"), "interviews.reschedule": ("mcp:propose", "interviews"),
    "interviews.cancel": ("mcp:propose", "interviews"),
}  # fmt: skip


def test_exposure_set_is_valid_and_complete() -> None:
    autodiscover("app.services")
    assert validate_exposures() == []
    mcp = exposures("mcp")
    assert {k: (e.mcp_scope, e.group) for k, e in mcp.items() if k in EXPECTED} == EXPECTED
    for key, exp in mcp.items():
        tool = tools()[key]
        assert tool.mcp_exposed, key
        assert exp.group in catalog.MCP_TOOL_GROUPS, key
        if tool.risk == "high" or tool.egress:
            assert exp.mcp_scope == "mcp:propose", key
        if tool.transition is not None:
            assert not wf.is_human_only(*tool.transition), key
        assert exp.max_results <= 50


def test_no_star_tool_and_no_star_scope() -> None:
    autodiscover("app.services")
    names = {t.wire_name for t, _ in catalog.exposed()}
    for entity, state in FORBIDDEN_ACTIONS:
        assert not any(entity in n and state in n for n in names)
    assert oauth_scopes.validate_catalogue() == []
    for key in ("knowledge:read", "predictions:read", "messages:propose", "interviews:propose"):
        spec = oauth_scopes.get_scope(key)
        assert spec.resources == frozenset({"mcp"}) and not spec.api_key_allowed
        assert not set(spec.permissions) & (STAR_PERMISSIONS | NON_DELEGABLE)


def test_every_exposed_permission_is_unlockable() -> None:
    autodiscover("app.services")
    unlockable: set[str] = set()
    for spec in oauth_scopes.scopes().values():
        if "mcp" in spec.resources or "api" in spec.resources:
            unlockable.update(spec.permissions)
    for tool, _exp in catalog.exposed():
        assert set(tool.permissions) <= unlockable, tool.key
