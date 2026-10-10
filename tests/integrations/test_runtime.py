"""Epic 66 (story 66.5 / 66.10, spec 0.6 / 0.10): the minimal install contract, the connector sync runner, the
Ecosystem Kill Switch gate on connector jobs, uninstall and the manifest diff."""

import dataclasses
import uuid

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden
from app.core.integrations import DataFlow, manifest, manifest_diff, manifest_sha256
from app.core.permissions import Principal, Scope
from app.models.connectors import ConnectorJob, ProviderConnectorConfig
from app.models.integrations import ConnectorSyncState
from app.models.vault import VaultSecret
from app.schemas.marketplace import InstallIn
from app.services import ecosystem_service, integrations_service, marketplace_service
from app.services.connectors import jobs
from tests.epic3 import Org3

KEY = "fake_job_board"


def _human(org: Org3) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type="user",
            id=org.admin.user_id,
            tenant_id=org.tenant_id,
            permissions={"integration:manage": Scope.TENANT, "ecosystem:kill_switch": Scope.TENANT},
        ),
    )


async def _install(org: Org3) -> uuid.UUID:
    m = manifest(KEY)
    data = InstallIn(
        kind="connector",
        connector_key=KEY,
        secrets={"api_key": "k-123456"},
        accountable_user_id=org.admin.user_id,
        accept_manifest_sha256=manifest_sha256(m),
    )
    async with tenant_session(org.tenant_id) as db:
        out = await marketplace_service.install(_human(org), db, data)
    assert out.status == "active" and len(out.config_ids) == 1
    return out.id


async def test_install_stores_configs_and_vault_secrets(org3: Org3) -> None:
    inst_id = await _install(org3)
    async with tenant_session(org3.tenant_id) as db:
        cfg = (await db.execute(select(ProviderConnectorConfig))).scalar_one()
        assert (cfg.installation_id, cfg.provider_key, cfg.slot_key) == (inst_id, KEY, KEY)
        secret = (await db.execute(select(VaultSecret))).scalar_one()
        assert (secret.owner_type, secret.owner_id, secret.name) == ("provider_config", cfg.id, "api_key")
        assert b"k-123456" not in secret.ciphertext


async def test_stale_consent_and_app_actor_refused(org3: Org3) -> None:
    data = InstallIn(
        kind="connector",
        connector_key=KEY,
        accountable_user_id=org3.admin.user_id,
        accept_manifest_sha256="0" * 64,
    )
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await marketplace_service.install(_human(org3), db, data)
    assert exc.value.code == "manifest_changed"
    app_ctx = RequestContext(
        tenant_id=org3.tenant_id,
        principal=Principal(
            type="app",
            id=uuid.uuid4(),
            tenant_id=org3.tenant_id,
            permissions={"integration:manage": Scope.TENANT},
        ),
    )
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(Forbidden) as exc2:
            await marketplace_service.install(app_ctx, db, data)
    assert exc2.value.code == "human_only_action"


async def test_sync_runner_and_kill_switch_gate(org3: Org3) -> None:
    inst_id = await _install(org3)
    seen: list[str] = []

    async def handler(ctx, db, run):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        ref = await run.adapter.field_schema()
        seen.append(str(len(ref.fields)))
        return integrations_service.SyncResult(processed=1, next_cursor={"page": 2})

    integrations_service.register_sync_handler(KEY, "publish", handler)
    try:
        async with tenant_session(org3.tenant_id) as db:
            await ecosystem_service.set_kill_switch(_human(org3), db, on=True, reason="incident")
        async with tenant_session(org3.tenant_id) as db:  # queued after commit (eager): deferred, not counted
            job = await integrations_service.run_sync(_human(org3), db, inst_id, "publish")
        async with tenant_session(org3.tenant_id) as db:
            status, delay = await jobs.execute(RequestContext(tenant_id=org3.tenant_id), db, job.id)
        assert (status, delay) == ("deferred", 300)
        async with tenant_session(org3.tenant_id) as db:
            row = await db.get(ConnectorJob, job.id)
            assert row is not None and row.attempts == 0 and row.status == "queued"
            assert not seen
            await ecosystem_service.set_kill_switch(_human(org3), db, on=False, reason="resolved")
        async with tenant_session(org3.tenant_id) as db:
            status, _ = await jobs.execute(RequestContext(tenant_id=org3.tenant_id), db, job.id)
        assert status == "succeeded" and seen
        async with tenant_session(org3.tenant_id) as db:
            row = await db.get(ConnectorJob, job.id)
            assert row is not None and row.result is not None and row.result["processed"] == 1
            state = (await db.execute(select(ConnectorSyncState))).scalar_one()
            assert state.cursor == {"page": 2} and state.consecutive_failures == 0
    finally:
        integrations_service.unregister_sync_handler(KEY, "publish")


async def test_uninstall_disables_configs_and_deletes_secrets(org3: Org3) -> None:
    inst_id = await _install(org3)
    async with tenant_session(org3.tenant_id) as db:
        out = await marketplace_service.uninstall(_human(org3), db, inst_id, reason="done")
    assert out.status == "uninstalled"
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(ProviderConnectorConfig.status))).scalar_one() == "disabled"
        assert (await db.execute(select(VaultSecret))).first() is None


def test_manifest_diff_flags_reconsent() -> None:
    old = manifest(KEY)
    same = manifest_diff(old, old)
    assert not same.requires_reconsent
    wider = dataclasses.replace(
        old,
        version=old.version + 1,
        egress_hosts=("api.example.test",),
        data_flows=(*old.data_flows, DataFlow("out", "candidate", ("email",), "matching", "IN", True)),
    )
    diff = manifest_diff(old, wider)
    assert (
        diff.requires_reconsent and diff.added_hosts == ("api.example.test",) and len(diff.added_flows) == 1
    )
