# ruff: noqa: E501
"""Epic 67 (story 67.4): refresh rotation, reuse detection, idle / absolute expiry, narrowing, concurrency."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.models.oauth import OAuthToken
from tests.epic3 import Org3
from tests.oauth.e67 import consent, exchange, install, public_get, refresh_call, register

SCOPES = ("candidates:read", "demands:read", "offline_access")


async def _tokens(app: FastAPI, org: Org3) -> tuple[dict, dict]:
    created = await register(org.admin, scopes=SCOPES)
    await install(org, created)
    code, verifier = await consent(org.admin, created, scope=" ".join(SCOPES))
    resp = await exchange(app, created, code, verifier)
    assert resp.status_code == 200, resp.text
    return created, resp.json()


async def test_rotation_and_reuse_detection(app: FastAPI, org3: Org3) -> None:
    created, first = await _tokens(app, org3)
    second = await refresh_call(app, created, first["refresh_token"])
    assert second.status_code == 200, second.text
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await public_get(app, second["access_token"])).status_code == 200
    # reuse of the rotated token -> invalid_grant and the whole family is revoked
    reuse = await refresh_call(app, created, first["refresh_token"])
    assert reuse.json()["error"] == "invalid_grant"
    assert (await public_get(app, second["access_token"])).status_code == 401
    assert (await refresh_call(app, created, second["refresh_token"])).json()["error"] == "invalid_grant"
    async with tenant_session(org3.tenant_id) as db:
        reasons = set((await db.execute(select(OAuthToken.revoked_reason))).scalars())
    assert "reuse_detected" in reasons


async def test_narrower_scope(app: FastAPI, org3: Org3) -> None:
    created, first = await _tokens(app, org3)
    resp = await refresh_call(app, created, first["refresh_token"], scope="candidates:read offline_access")
    assert resp.status_code == 200 and set(resp.json()["scope"].split()) == {"candidates:read", "offline_access"}
    wider = await refresh_call(app, created, resp.json()["refresh_token"], scope="candidates:read demands:read")
    assert wider.json()["error"] == "invalid_scope"


async def test_idle_and_absolute_expiry(app: FastAPI, org3: Org3) -> None:
    created, first = await _tokens(app, org3)
    past = datetime.now(UTC) - timedelta(days=31)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(OAuthToken).where(OAuthToken.kind == "refresh").values(created_at=past, last_used_at=past))
    assert (await refresh_call(app, created, first["refresh_token"])).json()["error"] == "invalid_grant"
    created, first = await _tokens(app, org3)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(
            update(OAuthToken).where(OAuthToken.kind == "refresh", OAuthToken.revoked_at.is_(None))
            .values(absolute_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )  # fmt: skip
    assert (await refresh_call(app, created, first["refresh_token"])).json()["error"] == "invalid_grant"


async def test_concurrent_refresh_one_wins(app: FastAPI, org3: Org3) -> None:
    created, first = await _tokens(app, org3)
    results = await asyncio.gather(*(refresh_call(app, created, first["refresh_token"]) for _ in range(2)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 400]
    winner = next(r for r in results if r.status_code == 200).json()
    assert (await public_get(app, winner["access_token"])).status_code == 401  # family revoked by the loser's reuse
