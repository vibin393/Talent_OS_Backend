# ruff: noqa: E501
"""After-response request log (``api_request_log``) + ``api_request`` metering; reconcile drift."""

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.database import tenant_session
from app.core.permissions import Principal
from app.models.metering import ApiRequestLog
from app.services import metering_service
from app.services.metering import record as rec
from app.services.metering import request_log


def _request(path: str = "/public/v1/candidates/{candidate_id}", **state: Any) -> SimpleNamespace:
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)},
        method="get",
        url=SimpleNamespace(path="/public/v1/candidates/123"),
        state=SimpleNamespace(**state),
    )


async def _run_hooks(req: SimpleNamespace, status: int) -> None:
    for hook in req.scope.get("tos_after_response", []):
        await hook(status)


async def test_api_key_request_is_logged_and_metered(tenant: uuid.UUID, clean_buffer: None) -> None:
    key = uuid.uuid4()
    principal = Principal(type="api_key", id=key, tenant_id=tenant, api_key_id=key)
    req = _request(tos_deprecated=True)
    metering_service.register_request_log(req, principal)
    metering_service.register_request_log(req, principal)  # idempotent
    assert len(req.scope["tos_after_response"]) == 1
    await _run_hooks(req, 404)
    async with tenant_session(tenant) as db:
        rows = list(
            (await db.execute(select(ApiRequestLog).where(ApiRequestLog.api_key_id == key))).scalars()
        )
    assert len(rows) == 1
    row = rows[0]
    assert (row.status, row.api_version, row.deprecated, row.method) == (404, "v1", True, "GET")
    assert row.route_template == "/public/v1/candidates/{candidate_id}"
    drained = rec.buffer_drain()
    (k, v), *_ = drained.items()
    assert k[2] == "api_request" and k[4] == str(key) and k[6] == "candidates"
    assert v[:2] == [1, 1]


async def test_app_request_logged(tenant: uuid.UUID, clean_buffer: None) -> None:
    inst, user, token, app = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    principal = Principal(
        type="app",
        id=inst,
        tenant_id=tenant,
        app_installation_id=inst,
        acting_user_id=user,
        oauth_token_id=token,
        app_id=app,
    )
    req = _request()
    metering_service.register_request_log(req, principal)
    await _run_hooks(req, 200)
    async with tenant_session(tenant) as db:
        row = (
            await db.execute(select(ApiRequestLog).where(ApiRequestLog.installation_id == inst))
        ).scalar_one()
    assert (row.user_id, row.token_id, row.deprecated) == (user, token, False)
    (k, v), *_ = rec.buffer_drain().items()
    assert k[3] == str(inst) and k[5] == str(app) and v[:2] == [1, 0]


class _FutureDt:
    UTC = dt.UTC

    class datetime:  # noqa: N801 - mirrors the module attribute
        @staticmethod
        def now(tz: Any = None) -> dt.datetime:  # noqa: ARG004
            return dt.datetime(2099, 1, 1, tzinfo=dt.UTC)


async def test_missing_partition_never_fails(
    tenant: uuid.UUID, clean_buffer: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(request_log, "dt", _FutureDt)
    key = uuid.uuid4()
    req = _request()
    metering_service.register_request_log(
        req, Principal(type="api_key", id=key, tenant_id=tenant, api_key_id=key)
    )
    await _run_hooks(req, 200)  # logs, does not raise
    assert rec.buffer_size() == 1  # still metered


async def test_reconcile_detects_drift(tenant: uuid.UUID, clean_buffer: None) -> None:
    today = dt.datetime.now(dt.UTC).date()
    key = uuid.uuid4()
    async with tenant_session(tenant) as db:
        await db.execute(
            text(
                "INSERT INTO api_request_log (id, tenant_id, at, api_key_id, api_version) "
                "SELECT gen_random_uuid(), :t, now(), :k, 'v1' FROM generate_series(1, 1000)"
            ),
            {"t": tenant, "k": key},
        )
        await db.execute(
            text(
                "INSERT INTO usage_counters (id, tenant_id, day, unit, api_key_id, route_group, count, created_at, "
                "updated_at) VALUES (gen_random_uuid(), :t, :d, 'api_request', :k, '', 1001, now(), now())"
            ),
            {"t": tenant, "k": key, "d": today},
        )
    drift = (await metering_service.reconcile(today, [tenant]))[str(tenant)]
    assert (drift.metered, drift.logged, drift.alert) == (1001, 1000, False)  # 0.0999 %
    async with tenant_session(tenant) as db:
        await db.execute(text("UPDATE usage_counters SET count = 1002 WHERE tenant_id = :t"), {"t": tenant})
    drift = (await metering_service.reconcile(today, [tenant]))[str(tenant)]
    assert drift.alert and drift.delta == 2
    async with tenant_session(tenant) as db:
        n = (await db.execute(select(func.count()).select_from(ApiRequestLog))).scalar_one()
    assert n == 1000
