# ruff: noqa: E501
"""Epic 67 Public API test helpers: an installed App with every v1 read/write scope, app-only and delegated tokens."""

import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import FastAPI

from app.models.oauth import AppInstallation
from tests.epic3 import Org3
from tests.modules.identity.conftest import new_client
from tests.support.oauth import install_app, make_app, mint

BASE = "/api/public/v1"
READ_SCOPES = (
    "candidates:read", "applications:read", "demands:read", "postings:read", "clients:read", "contacts:read",
    "users:read", "pools:read", "skills:read", "submissions:read", "interviews:read", "offers:read", "placements:read",
    "workers:read", "timesheets:read", "expenses:read", "invoices:read", "notes:read", "tasks:read", "activities:read",
)  # fmt: skip
WRITE_SCOPES = ("candidates:write", "applications:write", "demands:write", "postings:write", "notes:write",
                "tasks:write", "activities:write", "imports:write")  # fmt: skip
PII = ("pii.contact:read", "pii.financial:read", "pii.identity:read")
ALL = (*READ_SCOPES, *WRITE_SCOPES, *PII)


async def installed(org: Org3, scopes: Sequence[str] = ALL) -> AppInstallation:
    app = await make_app(scopes=scopes, resources=("api",))
    return await install_app(org.tenant_id, app, granted_by=org.admin.user_id)


async def app_token(inst: AppInstallation, scopes: Sequence[str] = (*READ_SCOPES, *WRITE_SCOPES)) -> str:
    return (await mint(inst, scopes=scopes)).access_token


async def user_token(inst: AppInstallation, user: uuid.UUID, scopes: Sequence[str] = ALL) -> str:
    return (await mint(inst, user=user, scopes=scopes)).access_token


class Api:
    def __init__(self, app: FastAPI, token: str) -> None:
        self.app, self.token = app, token

    def _h(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    async def get(self, path: str, **params: Any) -> httpx.Response:
        async with new_client(self.app) as c:
            return await c.get(BASE + path, params=params or None, headers=self._h())

    async def post(self, path: str, json: Any = None, *, key: str | None = "auto", headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        extra = dict(headers or {})
        if key == "auto":
            extra["Idempotency-Key"] = uuid.uuid4().hex
        elif key is not None:
            extra["Idempotency-Key"] = key
        async with new_client(self.app) as c:
            return await c.post(BASE + path, json=json, headers=self._h(extra), **kw)

    async def patch(self, path: str, json: Any, *, if_match: str | None = None) -> httpx.Response:
        extra = {"If-Match": if_match} if if_match is not None else {}
        async with new_client(self.app) as c:
            return await c.patch(BASE + path, json=json, headers=self._h(extra))


def code(resp: httpx.Response) -> str | None:
    body = resp.json() if resp.content else {}
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


CONSENT = {"basis": "notice_provided", "notice_version": "v1", "captured_at": "2026-10-10T10:00:00Z", "channel": "web_form"}
