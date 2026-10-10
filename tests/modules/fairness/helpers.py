# ruff: noqa: E501
"""Shared helpers of the Epic 49 fairness tests."""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from tests.modules.demands.helpers import DOrg, h
from tests.p2 import sign_in_candidate

API = "/api/v1"
F = f"{API}/fairness"
PORTAL = f"{API}/portal"
CSRF = {"X-CSRF-Token": "csrf-token"}


async def enable(org: DOrg, **kw: Any) -> dict[str, Any]:
    body = {"self_id_enabled": True, **kw}
    r = await org.admin.client.patch(f"{F}/settings", json=body, headers=h(org.admin))
    assert r.status_code == 200, r.text
    return dict(r.json())


async def make_candidate(org: DOrg, name: str = "Asha Candidate") -> uuid.UUID:
    email = f"cand-{uuid.uuid4().hex[:8]}@example.com"
    r = await org.admin.client.post(f"{API}/candidates", json={"full_name": name, "emails": [{"email": email}]}, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def portal(app: FastAPI, org: DOrg, candidate_id: uuid.UUID) -> httpx.AsyncClient:
    return await sign_in_candidate(app, candidate_id, org.tenant_id)


async def put_answers(client: httpx.AsyncClient, answers: dict[str, str]) -> httpx.Response:
    form = await client.get(f"{PORTAL}/self-id")
    assert form.status_code == 200, form.text
    notice_id = form.json()["notice"]["id"]
    return await client.put(f"{PORTAL}/self-id", json={"notice_id": notice_id, "answers": answers}, headers=CSRF)
