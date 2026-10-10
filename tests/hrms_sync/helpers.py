# ruff: noqa: E501
"""Epic 71 test harness (shared by ``tests/hrms_sync`` and ``tests/ledger_sync``): a tenant with its default Legal Entity
(open periods Dec 2025 - Mar 2027), a client and a signed-in Tenant Admin (the receivables ``ROrg``); connector installs
in Fake mode through the Epic 69 install flow (a passing test connection activates them); Placements inserted directly;
outbox -> ``connector_triggers`` sink -> connector jobs drained explicitly. Fakes only; no socket is opened."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import emit
from app.core.integrations import manifest, manifest_sha256
from app.core.permissions import CATALOG, Principal, Scope
from app.models.candidates import Candidate, CandidateEmail
from app.models.connectors import ConnectorJob, ProviderConnectorConfig
from app.models.placements import Placement
from app.models.public_events import OutboxEvent
from tests.modules.receivables.conftest import ROrg

SECRETS: dict[str, dict[str, str]] = {
    "darwinbox": {"api_key": "k-darwin", "username": "u", "password": "p"},
    "keka": {"client_id": "c", "client_secret": "s", "api_key": "k-keka"},
    "darwinbox_payroll": {"api_key": "k-darwin", "username": "u", "password": "p"},
    "keka_payroll": {"client_id": "c", "client_secret": "s", "api_key": "k-keka"},
    "zoho_books": {"oauth_refresh": "r-token", "client_id": "zc", "client_secret": "zs"},
    "tally_xml": {},
}


def human(org: ROrg, *perms: str, principal_type: str = "user") -> RequestContext:
    permissions = dict.fromkeys(perms, Scope.TENANT) if perms else dict.fromkeys(CATALOG, Scope.TENANT)
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type=principal_type, id=org.admin.user_id, tenant_id=org.tenant_id, permissions=permissions),  # type: ignore[arg-type]
    )


def system(org: ROrg) -> RequestContext:
    return RequestContext(tenant_id=org.tenant_id, actor_kind="system")


async def install(org: ROrg, key: str, *, config: dict[str, Any] | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    from app.schemas.marketplace import InstallIn
    from app.services import marketplace_service

    data = InstallIn(
        kind="connector", connector_key=key, secrets=SECRETS[key], accountable_user_id=org.admin.user_id,
        legal_entity_ids=[org.entity_id], config={"mode": "fake", **(config or {})}, accept_manifest_sha256=manifest_sha256(manifest(key)),
    )  # fmt: skip
    async with tenant_session(org.tenant_id) as db:
        out = await marketplace_service.install(human(org), db, data)
    assert out.status == "active", out
    async with tenant_session(org.tenant_id) as db:
        cfg = (await db.execute(select(ProviderConnectorConfig).where(ProviderConnectorConfig.installation_id == out.id))).scalars().first()
        assert cfg is not None
        return out.id, cfg.id


async def make_placement(
    org: ROrg,
    *,
    worker_type: str | None = "employee",
    payee_kind: str = "worker",
    status: str = "active",
    name: str = "Asha Verma",
    role_title: str = "Data Engineer",
    end_date: date | None = None,
) -> Placement:
    async with tenant_session(org.tenant_id) as db:
        cid = uuid.uuid4()
        db.add(Candidate(id=cid, tenant_id=org.tenant_id, human_id=f"C-{uuid.uuid4().hex[:8]}", full_name=name, source="manual", owner_id=org.admin.user_id))
        await db.flush()
        db.add(CandidateEmail(id=uuid.uuid4(), tenant_id=org.tenant_id, candidate_id=cid, email=f"{uuid.uuid4().hex[:6]}@example.test", is_primary=True))
        vendor_id = None
        if payee_kind == "vendor":
            from tests.p2 import make_vendor

            vendor_id = await make_vendor(org.tenant_id)
        p = Placement(
            id=uuid.uuid4(), tenant_id=org.tenant_id, human_id=f"PLC-{uuid.uuid4().hex[:6]}", candidate_id=cid, client_id=org.client_id,
            vendor_id=vendor_id, legal_entity_id=org.entity_id, worker_type=worker_type, payee_kind=payee_kind, status=status,
            planned_start_date=date(2026, 10, 1), actual_start_date=date(2026, 10, 1), end_date=end_date, currency="INR",
            role_title=role_title, work_location_text="Bengaluru",
        )  # fmt: skip
        db.add(p)
        await db.flush()
        await db.refresh(p)
        return p


async def fire(org: ROrg, event: Any) -> None:
    """Emit ``event`` (outbox written in the transaction), then feed the new outbox rows to the connector sink."""
    from app.services.integrations.triggers import connector_triggers_sink

    async with tenant_session(org.tenant_id) as db:
        before = (await db.execute(select(OutboxEvent.seq).order_by(OutboxEvent.seq.desc()).limit(1))).scalar_one_or_none() or 0
        emit(db, event)
    async with tenant_session(org.tenant_id) as db:
        rows = list((await db.execute(select(OutboxEvent).where(OutboxEvent.seq > before).order_by(OutboxEvent.seq))).scalars())
        await connector_triggers_sink(system(org), db, rows)


async def drain(org: ROrg) -> list[str]:
    """Execute every queued connector sync job of the tenant; returns the outcomes."""
    from app.services.connectors import jobs

    outcomes: list[str] = []
    for _ in range(5):
        async with tenant_session(org.tenant_id) as db:
            ids = list((await db.execute(select(ConnectorJob.id).where(ConnectorJob.status == "queued", ConnectorJob.op.like("sync:%")))).scalars())
        if not ids:
            break
        for job_id in ids:
            async with tenant_session(org.tenant_id) as db:
                status, _ = await jobs.execute(system(org), db, job_id)
                outcomes.append(status)
    return outcomes
