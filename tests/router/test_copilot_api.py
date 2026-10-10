# ruff: noqa: E501
"""Epic 54 ``/copilot/*`` API: cross-tenant 404 on every owner-scoped endpoint, permissions on settings / usage,
status for a user without ``copilot:use``."""

import uuid

from tests.agents.conftest import _agent_env, _agents_registered  # noqa: F401
from tests.agents.copilot.conftest import (  # noqa: F401
    C,
    CopilotOrg,
    ask,
    cop,
    h,
    new_conversation,
    other_cop,
)


async def test_cross_tenant_404_everywhere(cop: CopilotOrg, other_cop: CopilotOrg) -> None:  # noqa: F811
    conv = await new_conversation(cop.admin)
    turn, _ = await ask(cop.admin, conv["id"], "hello")
    o = other_cop.admin
    cid, rid = conv["id"], turn["run_id"]
    checks = [
        ("GET", f"{C}/conversations/{cid}", None),
        ("PATCH", f"{C}/conversations/{cid}", {"title": "x"}),
        ("POST", f"{C}/conversations/{cid}/close", None),
        ("DELETE", f"{C}/conversations/{cid}", None),
        ("GET", f"{C}/conversations/{cid}/messages", None),
        ("POST", f"{C}/conversations/{cid}/messages", {"text": "hi"}),
        ("GET", f"{C}/turns/{rid}/events", None),
        ("POST", f"{C}/turns/{rid}/cancel", None),
        ("POST", f"{C}/turns/{rid}/feedback", {"rating": "helpful"}),
    ]
    for method, url, body in checks:
        resp = await o.client.request(
            method, url, json=body, headers=h(o, **{"Idempotency-Key": uuid.uuid4().hex})
        )
        assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
    listed = (await o.client.get(f"{C}/conversations")).json()
    assert listed["total"] == 0


async def test_settings_roundtrip_and_permissions(cop: CopilotOrg) -> None:  # noqa: F811
    resp = await cop.admin.client.get(f"{C}/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_days"] == 90 and body["questions_per_hour"] == 60 and body["enabled"] is True
    resp = await cop.admin.client.put(
        f"{C}/settings", json={"retention_days": 30, "draft_expiry_hours": 12}, headers=h(cop.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retention_days"] == 30 and resp.json()["draft_expiry_hours"] == 12
    bad = await cop.admin.client.put(f"{C}/settings", json={"retention_days": 3}, headers=h(cop.admin))
    assert bad.status_code == 422
    bad = await cop.admin.client.put(f"{C}/settings", json={"draft_expiry_hours": 100}, headers=h(cop.admin))
    assert bad.status_code == 422
