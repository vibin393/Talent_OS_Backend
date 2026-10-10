# ruff: noqa: E501
"""Epic 69 (story 69.2, spec A4 / A6 / A9): test-gated install, ★ refusals, policy, private Apps, approvals, re-consent,
health, uninstall + erasure propagation, the processor view and the live-mode guard hook."""

import uuid

import pytest
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, ValidationFailed
from app.core.integrations import manifest, manifest_sha256
from app.models.connectors import ConnectorJob, ProviderConnectorConfig
from app.models.integrations import ConnectorLink
from app.models.marketplace import InstallationHealthLog
from app.models.oauth import AppGrant, AppInstallation
from app.models.vault import VaultSecret
from app.schemas.ecosystem import AppPolicyIn
from app.schemas.marketplace import ConfigureIn, InstallIn
from app.services import ecosystem_service, marketplace_service
from app.services.marketplace import health, reconsent
from tests.epic3 import Org3
from tests.marketplace.helpers import KEY, connector_in, human, install_connector, swapped_manifest
from tests.support.oauth import make_app

SECRET = "k-123456"


async def test_install_connector_is_test_gated_and_secret_free(org3: Org3) -> None:
    out = await install_connector(org3)
    assert out.status == "active" and out.test == {"ok": True, "code": None, "message": None}
    async with tenant_session(org3.tenant_id) as db:
        grant = (await db.execute(select(AppGrant).where(AppGrant.installation_id == out.id))).scalar_one()
        assert grant.data_flows and grant.data_flows[0]["entity"] == "job_posting"
        log = (await db.execute(select(InstallationHealthLog))).scalar_one()
        assert (log.trigger, log.result) == ("install", "ok")
        detail = await marketplace_service.get(human(org3), db, out.id)
        assert [s.name for s in detail.secrets if s.has_secret] == ["api_key"]
        assert SECRET not in detail.model_dump_json()
        # no plaintext anywhere but the vault ciphertext
        for table in ("app_installations", "app_grants", "provider_connector_configs", "audit_logs"):
            rows = (await db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t"))).scalars().all()
            assert not any(SECRET in r for r in rows), table


async def test_failed_test_stays_pending_then_test_again_promotes(org3: Org3) -> None:
    out = await install_connector(org3, api_key="invalid")
    assert out.status == "pending_consent" and out.test is not None and out.test["ok"] is False
    assert out.test["code"] == "auth_failed" and out.health_status == "failing"
    async with tenant_session(org3.tenant_id) as db:
        await marketplace_service.configure(
            human(org3), db, out.id, ConfigureIn(secrets={"api_key": "fixed-key"})
        )  # type: ignore[dict-item]
    async with tenant_session(org3.tenant_id) as db:
        result = await marketplace_service.test(human(org3), db, out.id)
    assert result.ok and result.status == "active"


async def test_star_refusals_and_stale_consent(org3: Org3) -> None:
    for kind in ("api_key", "app"):
        async with tenant_session(org3.tenant_id) as db:
            with pytest.raises(Forbidden) as exc:
                await marketplace_service.install(human(org3, kind), db, connector_in(org3))
        assert exc.value.code == "human_only_action"
    system = RequestContext(tenant_id=org3.tenant_id, actor_kind="system", principal=human(org3).principal)
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(Forbidden):
            await marketplace_service.install(system, db, connector_in(org3))
    stale = connector_in(org3).model_copy(update={"accept_manifest_sha256": "0" * 64})
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ConflictError) as exc2:
            await marketplace_service.install(human(org3), db, stale)
    assert exc2.value.code == "manifest_changed"


async def test_policy_blocks_publisher_and_private_apps(org3: Org3, other3: Org3) -> None:
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_app_policy(human(org3), db, AppPolicyIn(publisher_kinds=["partner"]))
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await marketplace_service.install(human(org3), db, connector_in(org3))
    assert exc.value.code == "publisher_not_allowed"
    # a private App of another tenant
    private = await make_app(publisher_kind="tenant", owner_tenant_id=other3.tenant_id, status="development")
    data = InstallIn(
        kind="app",
        app_id=private.id,
        scopes=["candidates:read"],
        accept_manifest_sha256=private.manifest_sha256,
    )
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ConflictError) as exc2:
            await marketplace_service.install(human(org3), db, data)
    assert exc2.value.code == "app_private"
    # own private App failing the automated checks (scope reason "test" < 20 chars)
    async with tenant_session(other3.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc3:
            await marketplace_service.install(human(other3), db, data)
    assert exc3.value.code == "app_checks_failed"
    assert any(c["key"] == "scope_reasons" for c in exc3.value.details["checks"])


async def test_sensitive_install_waits_for_the_approval_chain(org3: Org3) -> None:
    from app.core.oauth_scopes import is_sensitive, scopes
    from app.models.approvals import ApprovalChain, ApprovalChainStep
    from app.services import approvals

    sensitive = next(k for k in sorted(scopes()) if is_sensitive(k))
    app = await make_app(scopes=("candidates:read", sensitive))
    async with tenant_session(org3.tenant_id) as db:
        chain = ApprovalChain(
            id=uuid.uuid4(),
            tenant_id=org3.tenant_id,
            subject_type="installation_grant",
            name="Sensitive apps",
        )
        db.add(chain)
        await db.flush()
        db.add(ApprovalChainStep(id=uuid.uuid4(), tenant_id=org3.tenant_id, chain_id=chain.id, position=1, name="Admin",
                                 approver_kind="user", approver_user_id=org3.admin.user_id))  # fmt: skip
        await ecosystem_service.set_app_policy(
            human(org3), db, AppPolicyIn(sensitive_approval_chain_id=chain.id)
        )
    data = InstallIn(
        kind="app",
        app_id=app.id,
        scopes=["candidates:read", sensitive],
        accept_manifest_sha256=app.manifest_sha256,
    )
    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await marketplace_service.install(human(org3), db, data)
    assert exc.value.code == "purpose_required"
    data = data.model_copy(
        update={"sensitive_purposes": {sensitive: "We verify identities before onboarding."}}
    )
    async with tenant_session(org3.tenant_id) as db:
        out = await marketplace_service.install(human(org3), db, data)
    assert out.status == "pending_approval" and out.approval_request_id is not None
    async with tenant_session(org3.tenant_id) as db:
        from app.schemas.approvals import DecideIn

        await approvals.decide(human(org3), db, out.approval_request_id, DecideIn(action="approve"))
    async with tenant_session(org3.tenant_id) as db:
        inst = await db.get(AppInstallation, out.id)
        assert inst is not None and inst.status == "active"


async def test_reconsent_keeps_old_hosts_until_upgrade(org3: Org3) -> None:
    out = await install_connector(org3)
    original = manifest(KEY)
    with swapped_manifest(
        KEY, version=original.version + 1, egress_hosts=(*original.egress_hosts, "api.newboard.example")
    ) as new:
        async with tenant_session(org3.tenant_id) as db:
            assert await reconsent.apply_manifest_changes(human(org3), db) == 1
        async with tenant_session(org3.tenant_id) as db:
            inst = await db.get(AppInstallation, out.id)
            assert (
                inst is not None
                and inst.status == "reconsent_required"
                and inst.pending_manifest_version == new.version
            )
            assert await reconsent.consented_hosts(db, out.id, new) == list(original.egress_hosts)
            diff = await marketplace_service.upgrade_diff(human(org3), db, out.id)
            assert diff.added_hosts == ["api.newboard.example"] and diff.requires_reconsent
        async with tenant_session(org3.tenant_id) as db:
            up = await marketplace_service.upgrade(
                human(org3), db, out.id, accept_manifest_sha256=manifest_sha256(new)
            )
        assert up.status == "active" and up.pending_manifest_version is None
        async with tenant_session(org3.tenant_id) as db:
            grants = (
                (await db.execute(select(AppGrant).where(AppGrant.installation_id == out.id))).scalars().all()
            )
            assert len(grants) == 2 and sum(g.superseded_at is None for g in grants) == 1
            assert await reconsent.consented_hosts(db, out.id, new) is None


async def test_health_degrades_after_three_failures_and_recovers(org3: Org3) -> None:
    out = await install_connector(org3)
    async with tenant_session(org3.tenant_id) as db:
        assert not await health.on_sync_failed(db, out.id, 2, "provider_error")
        assert await health.on_sync_failed(db, out.id, 3, "provider_error")
    async with tenant_session(org3.tenant_id) as db:
        inst = await db.get(AppInstallation, out.id)
        assert inst is not None and inst.status == "degraded"
        outcome = await health.run_test(human(org3), db, inst, trigger="manual")
        assert outcome.ok and inst.status == "active"


async def test_uninstall_propagates_erasure_and_keeps_history(org3: Org3) -> None:
    with swapped_manifest(KEY, erasure=True) as m:
        async with tenant_session(org3.tenant_id) as db:
            out = await marketplace_service.install(
                human(org3),
                db,
                connector_in(org3).model_copy(update={"accept_manifest_sha256": manifest_sha256(m)}),
            )
        async with tenant_session(org3.tenant_id) as db:
            db.add(ConnectorLink(id=uuid.uuid4(), tenant_id=org3.tenant_id, installation_id=out.id, entity_type="candidate",
                                 local_id=uuid.uuid4(), external_id="ext-cand-1"))  # fmt: skip
            db.add(ConnectorJob(id=uuid.uuid4(), tenant_id=org3.tenant_id, category="job_board", op="sync:publish", subject_kind="installation",
                                subject_id=out.id, idempotency_key=f"q-{uuid.uuid4()}", status="queued", installation_id=out.id))  # fmt: skip
        async with tenant_session(org3.tenant_id) as db:
            job_id = await marketplace_service.propagate_erasure(human(org3), db, out.id)
            done = await marketplace_service.uninstall(human(org3), db, out.id, reason="Switching boards")
        assert done.status == "uninstalled" and job_id is not None
        async with tenant_session(org3.tenant_id) as db:
            job = await db.get(ConnectorJob, job_id)
            assert job is not None and job.op == "request_erasure" and job.status == "succeeded"
            assert job.payload == {"external_ids": {"candidate": ["ext-cand-1"]}}
            assert (await db.execute(select(VaultSecret))).first() is None
            assert set((await db.execute(select(ProviderConnectorConfig.status))).scalars()) == {"disabled"}
            statuses = set(
                (
                    await db.execute(select(ConnectorJob.status).where(ConnectorJob.op == "sync:publish"))
                ).scalars()
            )
            assert statuses == {"dead"}
            assert (await db.execute(select(ConnectorLink))).first() is not None  # history kept
            assert (await db.execute(select(AppGrant))).first() is not None


async def test_processor_view_and_data_flows(org3: Org3) -> None:
    out = await install_connector(org3)
    async with tenant_session(org3.tenant_id) as db:
        procs = await marketplace_service.list_processors(human(org3), db)
        mine = [p for p in procs if p.source == "installed"]
        assert [p.installation_id for p in mine] == [out.id] and mine[0].category == "job_boards"
        flows = await marketplace_service.data_flows(human(org3), db)
        assert {f.entity for f in flows} == {"job_posting", "board_application"}


async def test_live_mode_guard_hook(org3: Org3) -> None:
    calls: list[tuple[uuid.UUID | None, str]] = []

    async def guard(ctx, db, installation_id, key, mode) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        calls.append((installation_id, mode))
        raise ConflictError("Run a Fake burn-in first", code="burn_in_required")

    original = manifest(KEY)
    adapters = {**original.adapters, "live": original.adapters["fake"]}
    with swapped_manifest(KEY, adapters=adapters, egress_hosts=("api.board.example",)) as m:
        marketplace_service.register_live_mode_guard(guard)
        try:
            data = connector_in(org3).model_copy(
                update={"config": {"mode": "live"}, "accept_manifest_sha256": manifest_sha256(m)}
            )
            async with tenant_session(org3.tenant_id) as db:
                with pytest.raises(ConflictError) as exc:
                    await marketplace_service.install(human(org3), db, data)
            assert exc.value.code == "burn_in_required" and calls == [(None, "live")]
        finally:
            marketplace_service.unregister_live_mode_guard(guard)


async def test_cross_tenant_installation_is_not_found(org3: Org3, other3: Org3) -> None:
    from app.core.errors import NotFound

    out = await install_connector(org3)
    async with tenant_session(other3.tenant_id) as db:
        with pytest.raises(NotFound):
            await marketplace_service.get(human(other3), db, out.id)
