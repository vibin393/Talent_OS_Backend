"""AD-26: Client Users and Vendor Users never reach an internal route (Epic 13, story 13.5).

Every route of the app outside the portal / public / hook / sign-in prefixes is called with a valid client and
vendor session cookie (+ CSRF): the answer is always 401, 403 or 404, never 2xx."""

import re
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from tests.modules.demands.helpers import DOrg
from tests.p2 import External, make_client_user, make_vendor, make_vendor_user, sign_in_external

EXCLUDED_PREFIXES = (
    "/api/v1/client-portal",
    "/api/v1/vendor-portal",
    "/api/v1/portal",
    "/api/v1/public",
    "/api/v1/hooks",
    "/api/v1/auth",
    "/api/v1/platform/auth",  # operator sign-in (public)
    "/api/v1/health",
    "/api/health",
    "/api/pulse",
)
# Public endpoints outside those prefixes (no principal at all).
PUBLIC_PATHS = {
    ("GET", "/api/v1/sub-processors"),
    ("GET", "/api/v1/connections/{provider}/callback"),
    ("POST", "/api/v1/emails/webhooks/bounce"),
    ("POST", "/api/v1/emails/webhook"),
}
ALLOWED = {401, 403, 404}
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _url(path: str) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in ("entity_type", "entity"):
            return "candidates"
        if name == "provider":
            return "google"
        if name in ("slug", "token", "key", "category"):
            return "x"
        return str(uuid.uuid4())

    return _PARAM.sub(value, path)


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path.startswith(EXCLUDED_PREFIXES):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if (method, route.path) not in PUBLIC_PATHS:
                out.append((method, route.path))
    return out


@pytest.fixture
async def externals(app: FastAPI, dorg: DOrg) -> AsyncIterator[list[External]]:
    cu = await make_client_user(dorg.tenant_id, dorg.client_id)
    vendor = await make_vendor(dorg.tenant_id)
    vu = await make_vendor_user(dorg.tenant_id, vendor)
    out = [
        await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id),
        await sign_in_external(app, "vendor_user", vu, dorg.tenant_id, vendor),
    ]
    yield out
    for e in out:
        await e.client.aclose()


async def test_external_principals_never_reach_internal_routes(app: FastAPI, externals: list[External]) -> None:
    routes = _routes(app)
    assert len(routes) > 300
    offenders: list[str] = []
    for external in externals:
        me = await external.client.get(
            "/api/v1/client-portal/me" if external.principal_type == "client_user" else "/api/v1/vendor-portal/me"
        )
        assert me.status_code == 200, me.text  # the session itself is valid
        for method, path in routes:
            resp = await external.client.request(method, _url(path), headers=external.h(), json={} if method != "GET" else None)
            if resp.status_code not in ALLOWED:
                offenders.append(f"{external.principal_type} {method} {path} -> {resp.status_code}")
    assert offenders == []


def test_require_refuses_external_principals() -> None:
    import asyncio

    from app.core.errors import Forbidden
    from app.core.permissions import CATALOG, Principal, Scope, require

    dep = require("candidate:read")
    for kind in ("client_user", "vendor_user"):
        principal = Principal(type=kind, id=uuid.uuid4(), permissions=dict.fromkeys(CATALOG, Scope.TENANT))  # type: ignore[arg-type]
        with pytest.raises(Forbidden):
            asyncio.run(dep(principal))
