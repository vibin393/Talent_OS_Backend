# ruff: noqa: E501
"""Epic 73 (spec A7, NFR-64): the MCP red-team suite (``evals/mcp_redteam/*.yaml``) -- release gate, 100 % pass.

Each case scripts a hostile client against the real ``/api/mcp`` endpoint (ASGI, no network) with a seeded tenant whose
records carry injection payloads, and asserts outcomes. Marker ``evals`` (``pytest -m evals``)."""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.agent_runtime import AgentToolCall
from app.models.mcp import McpSession
from app.schemas.mcp import McpSettingsIn
from app.services import agent_runtime_service, mcp_service
from app.services.mcp_server import auth as mcp_auth
from app.services.mcp_server import limits, protocol
from tests.agents.conftest import AgentOrg
from tests.mcp.conftest import McpEnv, make_env
from tests.support.mcp_client import McpClient

pytestmark = pytest.mark.evals
SUITE = Path(__file__).resolve().parents[2] / "evals" / "mcp_redteam"
CASES = sorted(SUITE.glob("*.yaml"))


def test_suite_has_the_ten_cases() -> None:
    ids = {yaml.safe_load(p.read_text(encoding="utf-8"))["id"] for p in CASES}
    assert len(ids) == len(CASES) >= 12


def _fill(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


async def _seed(env: McpEnv, seed: dict[str, Any]) -> None:
    org = env.org
    if seed.get("title"):
        async with tenant_session(org.tenant_id) as db:
            await db.execute(text("UPDATE candidates SET current_title = :t WHERE id = :i"), {"t": seed["title"], "i": org.candidate_id})
    for note in seed.get("notes", []):
        resp = await org.admin.client.post(f"/api/v1/candidates/{org.candidate_id}/activities", json={"kind": note["kind"], "body": note["body"]}, headers=org.admin.h())
        assert resp.status_code == 201, resp.text


def _check_result(out: dict[str, Any], expect: dict[str, Any]) -> None:
    body = out["content"][0]["text"] if out.get("content") else ""
    if "is_error" in expect:
        assert out["isError"] is expect["is_error"], out
    if expect.get("untrusted"):
        assert out.get("_meta", {}).get("talentos/untrusted") is True, out
    for s in expect.get("text_contains", []):
        assert s in body, (s, body[:500])
    for s in expect.get("text_not_contains", []):
        assert s not in json.dumps(out), s
    if "text_equals" in expect:
        assert body == expect["text_equals"], body
    if "structured_status" in expect:
        assert out["structuredContent"]["status"] == expect["structured_status"], out


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
async def test_mcp_redteam_case(path: Path, app: FastAPI, agent_org: AgentOrg, other_org: AgentOrg) -> None:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = await make_env(app, agent_org)
    other = await make_env(app, other_org)
    values = {"candidate_id": str(agent_org.candidate_id), "other_candidate_id": str(other_org.candidate_id), "random_id": str(uuid.uuid4())}
    await _seed(env, case.get("seed") or {})
    client = await env.client()
    instructions = protocol.INSTRUCTIONS
    descriptions = {t["name"]: t["description"] for t in await client.list_tools()}
    if (case.get("seed") or {}).get("prefill_candidates"):
        key = hashlib.sha256(client.session_id.encode()).hexdigest()  # type: ignore[union-attr]
        await limits.add_candidates(key, [uuid.uuid4() for _ in range(int(case["seed"]["prefill_candidates"]))])
    try:
        for step in case["steps"]:
            step = _fill(step, values)
            expect = step.get("expect") or {}
            do = step["do"]
            if do == "call":
                _check_result(await client.call(step["tool"], step.get("args") or {}), expect)
            elif do == "instructions_static":
                init = await McpClient(app, client.token).initialize()
                assert init.json()["result"]["instructions"] == instructions
            elif do == "descriptions_static":
                assert {t["name"]: t["description"] for t in await client.list_tools()} == descriptions
            elif do == "description_suffix":
                for s in expect.get("text_contains", []):
                    assert s in descriptions[step["tool"]]
            elif do == "list_tools":
                names = {t["name"] for t in await client.list_tools()}
                assert set(expect.get("names_include", [])) <= names
                for word in expect.get("names_exclude_words", []):
                    assert not any(word in n for n in names), word
            elif do in ("rpc", "raw", "initialize", "new_client", "foreign_session"):
                if do == "rpc":
                    resp = await client.rpc(step["method"], step.get("params") or {})
                elif do == "raw":
                    raw = ("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\",\"params\":{\"x\":\"" + "a" * int(step["oversize"]) + "\"}}").encode() if step.get("oversize") else step["body"].encode()
                    hdrs = client.headers(session=not step.get("no_session"), extra=step.get("headers"))
                    resp = await client.http.post("/api/mcp", content=raw, headers=hdrs)
                elif do == "initialize":
                    resp = await McpClient(app, client.token).initialize(version=step["version"], expect_ok=False)
                elif do == "new_client":
                    mcp_auth.gate_cache.clear()
                    fresh = McpClient(app, await env.token(step.get("scopes") or ()) if step.get("scopes") else client.token)
                    resp = await fresh.initialize(expect_ok=not expect)
                    if not expect:
                        client = fresh
                        continue
                else:  # foreign_session: tenant B's session id with tenant A's token
                    foreign = await other.client()
                    hdrs = client.headers(session=False, extra={"Mcp-Session-Id": foreign.session_id or "", "MCP-Protocol-Version": foreign.negotiated or ""})
                    resp = await client.http.post("/api/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "ping"}, headers=hdrs)
                    await foreign.aclose()
                if "http" in expect:
                    assert resp.status_code == expect["http"], resp.text
                if "http_in" in expect:
                    assert resp.status_code in expect["http_in"], resp.text
                if "rpc_error" in expect:
                    assert resp.json()["error"]["code"] == expect["rpc_error"], resp.text
                if "run_error_code" in expect:
                    async with tenant_session(env.org.tenant_id) as db:
                        rows = (await db.execute(select(AgentToolCall).order_by(AgentToolCall.created_at.desc()))).scalars().all()
                        assert any(r.error_code == expect["run_error_code"] for r in rows), [(r.outcome, r.error_code) for r in rows]
            elif do == "set_groups":
                async with tenant_session(env.org.tenant_id) as db:
                    await mcp_service.update_settings(env.human_ctx(), db, McpSettingsIn(tool_groups=step["groups"]))
                mcp_auth.gate_cache.clear()
            elif do == "kill_switch":
                async with tenant_session(env.org.tenant_id) as db:
                    await agent_runtime_service.set_kill_switch(env.human_ctx(), db, on=True, reason="red team")
                mcp_auth.gate_cache.clear()
            elif do == "session_status":
                async with tenant_session(env.org.tenant_id) as db:
                    row = (await db.execute(select(McpSession).order_by(McpSession.started_at))).scalars().first()
                    assert row is not None
                    if "status" in expect:
                        assert (row.status, row.end_reason) == (expect["status"], expect.get("end_reason")), (row.status, row.end_reason)
                    if "min_redactions" in expect:
                        assert row.redaction_count >= expect["min_redactions"], row.redaction_count
            else:  # pragma: no cover - a typo in a case must fail loudly
                raise AssertionError(f"unknown step {do}")
    finally:
        await client.aclose()
        for c in env.clients + other.clients:
            await c.aclose()
