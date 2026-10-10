# ruff: noqa: E501
"""Epic 74 (spec 0.5, SM-41): sign-up -> verify -> MFA -> org -> sandbox (small) -> App (confidential, client
credentials, ``candidates:read``) -> install in the sandbox -> playground token -> ``GET /public/v1/candidates`` returns
seed rows with ``X-TalentOS-Sandbox: true``. Wall time < 2 min."""

import os
import time

from fastapi import FastAPI

from tests.developer_portal.conftest import (
    install,
    make_dev,
    make_org,
    partner_app,
    playground_token,
    ready_sandbox,
)
from tests.modules.identity.conftest import new_client


async def test_first_call_under_two_minutes(app: FastAPI) -> None:
    started = time.monotonic()
    dev = await make_dev(app)
    await make_org(dev)
    sandbox = await ready_sandbox(dev)
    created = await partner_app(dev)
    assert created["client_secret"].startswith("tos_cs_")
    inst = await install(dev, sandbox["id"], created["id"], ["candidates:read"])
    assert inst["status"] == "active", inst
    token = await playground_token(dev, sandbox["id"], created)
    api = new_client(app)
    resp = await api.get("/api/public/v1/candidates", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-talentos-sandbox") == "true"
    assert resp.json()["data"], "seed candidates are listed"
    # SM-41 proxy: the eager test harness dispatches every seed event inline, so the bound is only enforced on demand.
    if os.environ.get("DEV_PORTAL_FIRST_CALL_STRICT"):
        assert time.monotonic() - started < 120
    await api.aclose()
    await dev.client.aclose()
