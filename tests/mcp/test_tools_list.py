# ruff: noqa: E501
"""Epic 73 (spec 0.3, FR-377): ``tools/list`` = exposures ∩ enabled groups ∩ token scopes ∩ delegated rights, with
registry-generated, tenant-invariant metadata and no ★ action."""

from fastapi import FastAPI

from app.core.agentic.tools import RUNTIME_RESERVED_FIELDS
from app.core.agentic.types import FORBIDDEN_ACTIONS
from app.core.database import tenant_session
from app.schemas.mcp import McpSettingsIn
from app.services import mcp_service
from app.services.mcp_server import catalog
from tests.agents.conftest import AgentOrg
from tests.mcp.conftest import McpEnv, make_env

STAR_WORDS = ("reject", "release", "offer", "scorecard", "approve", "decide", "outcome", "invoice", "payment", "submit")


async def test_full_list_suffixes_schemas_and_no_star(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    listed = await c.list_tools()
    names = {t["name"] for t in listed}
    assert {"candidates__search", "candidates__get", "sla__at_risk", "matching__rank", "navigation__screen_for", "messaging__send"} <= names
    for t in listed:
        assert t["name"].replace("_", "").isalnum() and t["name"] == t["name"].lower()
        assert t["description"].endswith(tuple(catalog.SUFFIX.values())), t["name"]
        props = t["inputSchema"].get("properties", {})
        assert not set(props) & RUNTIME_RESERVED_FIELDS, t["name"]
        assert not any(w in t["name"] for w in STAR_WORDS), t["name"]
        assert set(t["annotations"]) >= {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    for entity, state in FORBIDDEN_ACTIONS:
        assert not any(entity in n and state in n for n in names)
    by_name = {t["name"]: t for t in listed}
    assert by_name["messaging__send"]["description"].endswith(catalog.SUFFIX["propose"])
    assert by_name["notes__add"]["description"].endswith(catalog.SUFFIX["draft"])
    assert by_name["candidates__get"]["annotations"]["readOnlyHint"] is True
    assert "nextCursor" not in await c.result("tools/list")


async def test_scope_group_and_permission_filters(mcp_env: McpEnv) -> None:
    read_only = await mcp_env.client(scopes=("mcp:read", "candidates:read"))
    names = {t["name"] for t in await read_only.list_tools()}
    assert "candidates__get" in names and "notes__add" not in names and "messaging__send" not in names
    assert "demands__list" not in names  # no demands:read -> the delegated rights lack demand:read
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await mcp_service.update_settings(mcp_env.human_ctx(), db, McpSettingsIn(tool_groups=["core"]))
    from app.services.mcp_server import auth as mcp_auth

    mcp_auth.gate_cache.clear()
    core = await mcp_env.client()
    groups = {catalog.lookup(t["name"])[1].group for t in await core.list_tools()}  # type: ignore[index]
    assert groups == {"core"}


async def test_list_is_identical_across_tenants(mcp_env: McpEnv, app: FastAPI, other_org: AgentOrg) -> None:
    other = await make_env(app, other_org)
    a = await (await mcp_env.client()).list_tools()
    b_client = await other.client()
    b = await b_client.list_tools()
    assert a == b
    await b_client.aclose()
