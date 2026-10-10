# ruff: noqa: E501
"""Epic 54 Copilot test harness: a tenant with ``agentic`` + ``agent_copilot`` on, its Tenant Admin, a recruiter
(team-scoped default role), a candidate, a client, and HTTP helpers. Model calls use the scripted Fake LLM (mapping
``{"agent_copilot": [...]}`` so title generation never consumes the agent's turns)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.services import ai
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.epic3 import create_candidate
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

C = "/api/v1/copilot"


@dataclasses.dataclass
class CopilotOrg:
    tenant_id: uuid.UUID
    tenant: Any
    admin: Member
    candidate_id: uuid.UUID
    client_id: uuid.UUID


def h(m: Member, **extra: str) -> dict[str, str]:
    return {**csrf(m.client), **extra}


async def enable(tenant_id: uuid.UUID, **features: bool) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_copilot": True, **features})


async def _client(m: Member) -> uuid.UUID:
    resp = await m.client.post(
        "/api/v1/clients", json={"name": f"Client {uuid.uuid4().hex[:6]}"}, headers=h(m)
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def make_org(app: FastAPI, name: str = "Ada Admin") -> CopilotOrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    await enable(tenant.id)
    cand = await create_candidate(
        admin, full_name="Priya Raman", emails=[{"email": "priya.raman@example.com"}]
    )
    return CopilotOrg(
        tenant_id=tenant.id,
        tenant=tenant,
        admin=admin,
        candidate_id=uuid.UUID(cand["id"]),
        client_id=await _client(admin),
    )


@pytest.fixture
async def cop(app: FastAPI) -> AsyncIterator[CopilotOrg]:
    org = await make_org(app)
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_cop(app: FastAPI) -> AsyncIterator[CopilotOrg]:
    org = await make_org(app, "Olga Other")
    yield org
    await org.admin.client.aclose()


def script(*turns: dict[str, Any]) -> dict[str, list[FakeTurn]]:
    return {"agent_copilot": [FakeTurn.from_mapping(t) for t in turns]}


async def new_conversation(m: Member, **body: Any) -> dict[str, Any]:
    resp = await m.client.post(f"{C}/conversations", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def ask(
    m: Member, conversation_id: str, text: str, turns: Sequence[dict[str, Any]] | None = None, **body: Any
) -> tuple[dict[str, Any], Any]:
    """Post a question with the Fake LLM playing ``turns`` (eager Celery: the turn completes inside the POST)."""
    with fake_llm(script(*(turns or [{"kind": "text", "text": "OK."}]))) as fake:
        resp = await m.client.post(
            f"{C}/conversations/{conversation_id}/messages",
            json={"text": text, **body},
            headers=h(m, **{"Idempotency-Key": uuid.uuid4().hex}),
        )
    assert resp.status_code == 200, resp.text
    return dict(resp.json()), fake


async def messages(m: Member, conversation_id: str) -> list[dict[str, Any]]:
    resp = await m.client.get(f"{C}/conversations/{conversation_id}/messages")
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def answer(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(m for m in reversed(msgs) if m["role"] == "assistant")


async def sse_events(
    m: Member, run_id: str, last_event_id: str | None = None
) -> tuple[list[tuple[str, str, Any]], Any]:
    import json

    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    resp = await m.client.get(f"{C}/turns/{run_id}/events", headers=headers, timeout=30)
    out: list[tuple[str, str, Any]] = []
    for block in resp.text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if "event" in fields:
            out.append((fields.get("id", ""), fields["event"], json.loads(fields.get("data") or "{}")))
    return out, resp
