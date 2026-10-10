# ruff: noqa: E501
"""Epic 62 test seeds (shared by tests/modules/{knowledge,chasing}, tests/agents/{compliance,bgv,onboarding} and the
eval seeds). Records are inserted directly where the P2 flows are not under test; nothing is ever sent to a real
address or network."""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services import ai
from tests.modules.demands.helpers import DOrg, create_client, h


@dataclasses.dataclass
class E62Seed:
    tenant_id: uuid.UUID
    admin: Any
    client_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID
    item_id: uuid.UUID

    def values(self) -> dict[str, str]:
        return {
            "candidate_id": str(self.candidate_id), "placement_id": str(self.placement_id), "item_id": str(self.item_id),
            "client_id": str(self.client_id), "owner_id": str(self.admin.user_id),
        }  # fmt: skip


def sys_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def enable(tenant_id: uuid.UUID, *agents: str, on: bool = True) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, **{f"agent_{a}": on for a in agents}})


async def grant_ai(tenant_id: uuid.UUID, candidate_id: uuid.UUID, *purposes: str) -> None:
    from app.services import privacy

    async with tenant_session(tenant_id) as db:
        for p in purposes or ("ai_processing",):
            await privacy.grant_consent(
                sys_ctx(tenant_id), db, candidate_id, p, source="conversation", evidence={"test": True}
            )


async def seed_worker(
    org: Any,
    *,
    start_in: int = 7,
    item_status: str = "missing",
    candidate_id: uuid.UUID | None = None,
    status: str = "pre_joining",
) -> E62Seed:
    """Create a Placement starting in ``start_in`` days for the org's candidate, with one mandatory "PAN card" item."""
    from tests.modules.legal_entities.conftest import provision
    from tests.modules.placements.helpers import direct_placement

    async with tenant_session(org.tenant_id) as db:
        has_le = (await db.execute(text("SELECT 1 FROM legal_entities LIMIT 1"))).first() is not None
    if not has_le:
        await provision(org.tenant)
    client_id = await create_client(org.admin, f"Acme {uuid.uuid4().hex[:6]}")
    dorg = DOrg(tenant=org.tenant, admin=org.admin, client_id=client_id)
    cid = candidate_id or org.candidate_id
    p = await direct_placement(
        dorg, cid, planned_start_date=(date.today() + timedelta(days=start_in)).isoformat()
    )
    pid = uuid.UUID(p["id"])
    item_id = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO placement_compliance_items (id, tenant_id, placement_id, candidate_id, requirement_ids, kind, ref, label, mandatory, status) "
                "VALUES (:id, :t, :p, :c, '{}'::uuid[], 'document', 'pan', 'PAN card', true, :s)"
            ),
            {"id": item_id, "t": org.tenant_id, "p": pid, "c": cid, "s": item_status},
        )
        if status != "pre_joining":
            await db.execute(text("UPDATE placements SET status = :s WHERE id = :i"), {"s": status, "i": pid})
    resp = await org.admin.client.patch(
        "/api/v1/onboarding/settings",
        json={"default_hr_owner_id": str(org.admin.user_id)},
        headers=h(org.admin),
    )
    assert resp.status_code == 200, resp.text
    return E62Seed(org.tenant_id, org.admin, client_id, cid, pid, item_id)


async def open_portal(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
    """Pretend the Worker opened the portal (a candidate session seen now)."""
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO sessions (id, token_hash, principal_type, principal_id, tenant_id, created_at, last_seen_at, expires_at) "
                "VALUES (:id, :th, 'candidate', :c, :t, now(), now(), now() + interval '1 day')"
            ),
            {
                "id": uuid.uuid4(),
                "th": uuid.uuid4().hex + uuid.uuid4().hex,
                "c": candidate_id,
                "t": tenant_id,
            },
        )


@dataclasses.dataclass
class BgvSeed:
    case_id: uuid.UUID
    check_id: uuid.UUID
    provider_id: uuid.UUID
    candidate_id: uuid.UUID


async def seed_bgv(
    org: Any,
    *,
    overdue_days: int = 3,
    connector: bool = False,
    email: str | None = "verify@provider.example.com",
    prior: int = 0,
) -> BgvSeed:
    from app.models.bgv import BgvCase, BgvCheck, BgvPackage, BgvProvider
    from app.models.chasing import BgvFollowUp

    case_id, check_id, provider_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        config_id = None
        if connector:
            from app.models.connectors import ProviderConnectorConfig

            config_id = uuid.uuid4()
            db.add(
                ProviderConnectorConfig(
                    id=config_id,
                    tenant_id=org.tenant_id,
                    category="bgv",
                    provider_key="fake",
                    display_name="Fake BGV",
                )
            )
            await db.flush()
        package = BgvPackage(
            id=uuid.uuid4(), tenant_id=org.tenant_id, name=f"Std {uuid.uuid4().hex[:4]}", checks=[]
        )
        db.add(package)
        db.add(
            BgvProvider(
                id=provider_id,
                tenant_id=org.tenant_id,
                name="VerifyCo",
                kind="external",
                contact_email=email,
                connector_config_id=config_id,
            )
        )
        await db.flush()
        db.add(
            BgvCase(
                id=case_id, tenant_id=org.tenant_id, human_id=f"BGV-{uuid.uuid4().hex[:6]}", candidate_id=org.candidate_id,
                package_id=package.id, package_snapshot={}, status="in_verification", created_by=org.admin.user_id,
            )
        )  # fmt: skip
        await db.flush()
        db.add(
            BgvCheck(
                id=check_id, tenant_id=org.tenant_id, case_id=case_id, candidate_id=org.candidate_id, type="employment",
                subject_label="Employment 1", status="in_progress", assignee_kind="provider", provider_id=provider_id,
                external_ref="EXT-42", started_at=datetime.now(UTC) - timedelta(days=10),
                due_on=date.today() - timedelta(days=overdue_days), overdue=True,
            )
        )  # fmt: skip
        await db.flush()
        for seq in range(1, prior + 1):
            db.add(
                BgvFollowUp(
                    id=uuid.uuid4(), tenant_id=org.tenant_id, check_id=check_id, case_id=case_id, candidate_id=org.candidate_id,
                    provider_id=provider_id, channel="email", seq=seq, status="sent", sent_at=datetime.now(UTC) - timedelta(days=30 - seq),
                )
            )  # fmt: skip
    return BgvSeed(case_id, check_id, provider_id, org.candidate_id)


async def publish_article(
    tenant_id: uuid.UUID, admin_ctx: RequestContext, title: str, body: str, audience: str = "both"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create + publish an article; returns (source_id, first chunk id)."""
    from sqlalchemy import select

    from app.models.knowledge import KnowledgeChunk
    from app.schemas.knowledge import SourceCreate
    from app.services import knowledge_service

    async with tenant_session(tenant_id) as db:
        src = await knowledge_service.sources.create_source(
            admin_ctx, db, SourceCreate(title=title, audience=audience, body_md=body)
        )  # type: ignore[arg-type]
        await knowledge_service.sources.publish(admin_ctx, db, src.id, 1)
        chunk_id = (
            await db.execute(
                select(KnowledgeChunk.id)
                .where(KnowledgeChunk.source_id == src.id)
                .order_by(KnowledgeChunk.seq)
                .limit(1)
            )
        ).scalar_one()
    return src.id, chunk_id


async def start_thread(tenant_id: uuid.UUID, candidate_id: uuid.UUID, question: str) -> Any:
    from app.services import knowledge_service

    async with tenant_session(tenant_id) as db:
        return await knowledge_service.qa.ask(db, tenant_id, candidate_id, question)
