"""AD-29 provider connector framework (Epic 13, story 13.9)."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.connectors import ConnectorJobFailed
from app.models.connectors import ConnectorInboundEvent, ConnectorJob
from app.services.connectors import jobs, registry
from app.services.connectors.esign.fake import FakeEsignProvider
from tests.modules.identity.conftest import csrf
from tests.modules.legal_entities.conftest import API, LeOrg

SECRET = "whsec-test-secret"


@pytest.fixture(autouse=True)
def _reset_fake() -> Iterator[None]:
    FakeEsignProvider.reset()
    yield
    FakeEsignProvider.reset()


async def _config(org: LeOrg, **extra: object) -> dict[str, object]:
    body = {
        "category": "esign",
        "provider_key": "fake",
        "display_name": "Fake eSign",
        "credentials": {"api_key": "sk-very-secret-value"},
        "webhook_secret": SECRET,
        **extra,
    }
    resp = await org.admin.client.post(f"{API}/connectors/configs", json=body, headers=csrf(org.admin.client))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_config_credentials_never_returned(leorg: LeOrg) -> None:
    created = await _config(leorg)
    assert created["has_credentials"] is True and created["has_webhook_secret"] is True
    listed = await leorg.admin.client.get(f"{API}/connectors/configs")
    assert listed.status_code == 200
    raw = listed.text + json.dumps(created)
    assert "sk-very-secret-value" not in raw and SECRET not in raw
    patched = await leorg.admin.client.patch(
        f"{API}/connectors/configs/{created['id']}",
        json={"credentials": {"api_key": "sk-another"}, "display_name": "Renamed"},
        headers=csrf(leorg.admin.client),
    )
    assert patched.status_code == 200 and "sk-another" not in patched.text
    assert patched.json()["display_name"] == "Renamed"
    async with tenant_session(leorg.tenant_id) as db:
        stored = (
            await db.execute(text("SELECT ciphertext FROM vault_secrets WHERE name = 'credentials'"))
        ).scalar_one()
        assert stored and b"sk-another" not in bytes(stored)
    dup = await leorg.admin.client.post(
        f"{API}/connectors/configs",
        json={"category": "esign", "provider_key": "fake", "display_name": "Dup"},
        headers=csrf(leorg.admin.client),
    )
    assert dup.status_code == 409


async def test_registry_falls_back_to_manual(leorg: LeOrg) -> None:
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        assert (await registry.get(ctx, db, "esign")).provider_key == "manual"
        assert (await registry.get(ctx, db, "bgv", leorg.entity_id)).provider_key == "manual"
    await _config(leorg)
    async with tenant_session(leorg.tenant_id) as db:
        adapter = await registry.get(ctx, db, "esign", leorg.entity_id)
        assert adapter.provider_key == "fake"
        assert adapter.config is not None and adapter.config.credentials == {
            "api_key": "sk-very-secret-value"
        }  # type: ignore[attr-defined]


async def test_enqueue_dedupes_and_runs_after_commit(leorg: LeOrg) -> None:
    await _config(leorg)
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    subject = uuid.uuid4()
    async with tenant_session(leorg.tenant_id) as db:
        a = await jobs.enqueue(ctx, db, "esign", "create_request", "signature_request", subject, 1, {"x": 1})
        b = await jobs.enqueue(ctx, db, "esign", "create_request", "signature_request", subject, 1, {"x": 2})
        assert a.id == b.id and a.idempotency_key == f"esign:create_request:signature_request:{subject}:1"
    async with tenant_session(leorg.tenant_id) as db:
        job = await db.get(ConnectorJob, a.id)
        assert job is not None and job.status == "succeeded" and job.attempts == 1
        assert job.result is not None and job.result["ok"] is True
        count = (
            (await db.execute(select(ConnectorJob).where(ConnectorJob.subject_id == subject))).scalars().all()
        )
        assert len(count) == 1
    assert [c[0] for c in FakeEsignProvider.calls] == ["create_request"]


async def test_manual_provider_job_succeeds_with_manual_result(leorg: LeOrg) -> None:
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        job = await jobs.enqueue(ctx, db, "bgv", "initiate", "bgv_case", uuid.uuid4(), 1, {})
    async with tenant_session(leorg.tenant_id) as db:
        row = await db.get(ConnectorJob, job.id)
        assert (
            row is not None
            and row.status == "succeeded"
            and row.result is not None
            and row.result["manual"] is True
        )


async def test_failing_job_retries_then_dead_and_event(leorg: LeOrg) -> None:
    await _config(leorg)
    FakeEsignProvider.fail_ops.add("explode")
    failed: list[ConnectorJobFailed] = []
    subscribe(ConnectorJobFailed, failed.append)
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    try:
        async with tenant_session(leorg.tenant_id) as db:
            job = await jobs.enqueue(ctx, db, "esign", "explode", "signature_request", uuid.uuid4(), 1, {})
    finally:
        unsubscribe(ConnectorJobFailed, failed.append)
    async with tenant_session(leorg.tenant_id) as db:
        row = await db.get(ConnectorJob, job.id)
        assert row is not None and row.status == "dead" and row.attempts == row.max_attempts == 5
        assert row.last_error and "fake explode failure" in row.last_error
    assert [e.job_id for e in failed] == [job.id]
    FakeEsignProvider.fail_ops.clear()
    async with tenant_session(leorg.tenant_id) as db:
        retried = await jobs.retry(ctx, db, job.id)
        assert retried.status == "queued"
    async with tenant_session(leorg.tenant_id) as db:
        row = await db.get(ConnectorJob, job.id)
        assert row is not None and row.status == "succeeded"
        assert await jobs.pending(ctx, db, "esign") == []


async def test_webhook_404_401_202(leorg: LeOrg) -> None:
    created = await _config(leorg)
    client = leorg.admin.client
    body = json.dumps({"status": "signed"}).encode()
    unknown = await client.post(f"{API}/hooks/esign/{uuid.uuid4()}", content=body)
    assert unknown.status_code == 404
    wrong_category = await client.post(f"{API}/hooks/bgv/{created['id']}", content=body)
    assert wrong_category.status_code == 404
    bad = await client.post(
        f"{API}/hooks/esign/{created['id']}", content=body, headers={"X-Fake-Signature": "nope"}
    )
    assert bad.status_code == 401
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    ok = await client.post(
        f"{API}/hooks/esign/{created['id']}", content=body, headers={"X-Fake-Signature": sig}
    )
    assert ok.status_code == 202, ok.text
    event_id = uuid.UUID(ok.json()["event_id"])
    async with tenant_session(leorg.tenant_id) as db:
        event = await db.get(ConnectorInboundEvent, event_id)
        assert event is not None and event.payload == {"status": "signed"}
        assert event.status == "processed"


async def test_jobs_api_requires_permission_and_lists(leorg: LeOrg) -> None:
    ctx = RequestContext(tenant_id=leorg.tenant_id)
    async with tenant_session(leorg.tenant_id) as db:
        await jobs.enqueue(ctx, db, "sms", "send", "notification", uuid.uuid4(), 1, {})
    resp = await leorg.admin.client.get(
        f"{API}/connectors/jobs", params={"category": "sms", "status": "succeeded"}
    )
    assert resp.status_code == 200 and len(resp.json()) == 1
