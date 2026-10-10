"""AD-23 / NFR-25: ``Idempotency-Key`` -- replay, hash mismatch, concurrent duplicates, abandoned takeover
(Epic 13)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.idempotency import IdempotencyGuard, idempotency, request_hash
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

PATH = "/api/v1/_t13/things"
executions: list[str] = []


class ThingIn(BaseModel):
    value: int
    slow: bool = False


@pytest.fixture(scope="module")
def idem_app() -> FastAPI:
    from app.main import create_app

    app = create_app()

    @app.post(PATH, status_code=status.HTTP_201_CREATED)
    async def create_thing(
        body: ThingIn,
        _: CurrentUserDep,
        db: TenantDBSessionDep,
        idem: Annotated[IdempotencyGuard, Depends(idempotency())],
    ) -> Any:
        async def work() -> dict[str, Any]:
            executions.append(str(body.value))
            if body.slow:
                await asyncio.sleep(0.4)
            await db.execute(
                text(
                    "INSERT INTO tenant_sequences (tenant_id, key, value) "
                    "VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, 't13-idem', 1) "
                    "ON CONFLICT (tenant_id, key) DO UPDATE SET value = tenant_sequences.value + 1"
                )
            )
            return {"value": body.value, "id": str(uuid.uuid4())}

        return await idem.run(work)

    @app.post("/api/v1/_t13/optional")
    async def optional(
        _: CurrentUserDep,
        db: TenantDBSessionDep,
        idem: Annotated[IdempotencyGuard, Depends(idempotency(required=False))],
    ) -> Any:
        async def work() -> dict[str, str]:
            return {"ok": "yes"}

        return await idem.run(work)

    return app


@pytest.fixture
async def member(idem_app: FastAPI) -> AsyncIterator[Member]:
    tenant = await make_tenant()
    m = await join(idem_app, tenant)
    yield m
    await m.client.aclose()


def _headers(m: Member, key: str | None) -> dict[str, str]:
    h = csrf(m.client)
    if key is not None:
        h["Idempotency-Key"] = key
    return h


async def _count(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as s:
        value = (await s.execute(text("SELECT value FROM tenant_sequences WHERE key = 't13-idem'"))).scalar_one_or_none()
    return int(value or 0)


async def test_replay_same_body(member: Member) -> None:
    key = uuid.uuid4().hex
    first = await member.client.post(PATH, json={"value": 1}, headers=_headers(member, key))
    second = await member.client.post(PATH, json={"value": 1}, headers=_headers(member, key))
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert await _count(member.tenant_id) == 1


async def test_reused_key_with_different_body(member: Member) -> None:
    key = uuid.uuid4().hex
    assert (await member.client.post(PATH, json={"value": 1}, headers=_headers(member, key))).status_code == 201
    resp = await member.client.post(PATH, json={"value": 2}, headers=_headers(member, key))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_key_reused"
    assert await _count(member.tenant_id) == 1


async def test_key_required(member: Member) -> None:
    resp = await member.client.post(PATH, json={"value": 1}, headers=_headers(member, None))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_required"
    ok = await member.client.post("/api/v1/_t13/optional", json={}, headers=_headers(member, None))
    assert ok.status_code == 200 and ok.json() == {"ok": "yes"}


async def test_concurrent_duplicates_execute_once(member: Member) -> None:
    key = uuid.uuid4().hex
    executions.clear()
    a, b = await asyncio.gather(
        member.client.post(PATH, json={"value": 5, "slow": True}, headers=_headers(member, key)),
        member.client.post(PATH, json={"value": 5, "slow": True}, headers=_headers(member, key)),
    )
    assert {a.status_code, b.status_code} == {201}
    assert a.json() == b.json()
    assert executions == ["5"]
    assert await _count(member.tenant_id) == 1


async def test_abandoned_key_is_taken_over(member: Member) -> None:
    key = uuid.uuid4().hex
    body = {"value": 9}
    digest = request_hash("POST", PATH, json.dumps(body).encode())
    async with tenant_session(member.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO idempotency_keys (id, tenant_id, principal_type, principal_id, key, request_hash, method, "
                "path, expires_at, created_at, updated_at) VALUES (:id, :t, 'user', :p, :k, :h, 'POST', :path, :exp, :old, :old)"
            ),
            {
                "id": uuid.uuid4(),
                "t": member.tenant_id,
                "p": member.user_id,
                "k": key,
                "h": digest,
                "path": PATH,
                "exp": datetime.now(UTC) + timedelta(hours=20),
                "old": datetime.now(UTC) - timedelta(minutes=10),
            },
        )
    resp = await member.client.post(PATH, json=body, headers=_headers(member, key))
    assert resp.status_code == 201
    assert await _count(member.tenant_id) == 1
    replay = await member.client.post(PATH, json=body, headers=_headers(member, key))
    assert replay.json() == resp.json()


async def test_in_progress_key_conflicts(member: Member) -> None:
    key = uuid.uuid4().hex
    body = {"value": 3}
    digest = request_hash("POST", PATH, json.dumps(body).encode())
    async with tenant_session(member.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO idempotency_keys (id, tenant_id, principal_type, principal_id, key, request_hash, method, "
                "path, expires_at) VALUES (:id, :t, 'user', :p, :k, :h, 'POST', :path, :exp)"
            ),
            {
                "id": uuid.uuid4(),
                "t": member.tenant_id,
                "p": member.user_id,
                "k": key,
                "h": digest,
                "path": PATH,
                "exp": datetime.now(UTC) + timedelta(hours=20),
            },
        )
    resp = await member.client.post(PATH, json=body, headers=_headers(member, key))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_key_in_progress"


async def test_purge_expired(member: Member) -> None:
    from app.core.idempotency import purge_expired

    key = uuid.uuid4().hex
    assert (await member.client.post(PATH, json={"value": 1}, headers=_headers(member, key))).status_code == 201
    async with tenant_session(member.tenant_id) as s:
        assert await purge_expired(s, datetime.now(UTC) + timedelta(hours=25)) >= 1
        left = (await s.execute(text("SELECT count(*) FROM idempotency_keys WHERE key = :k"), {"k": key})).scalar_one()
    assert left == 0
