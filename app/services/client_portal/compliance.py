"""Client compliance packs (Epic 18 A5, FR-103 last bullet).

Internal (Client 360): create a monthly pack, add items (scanned upload through ``files_service.store_upload`` or
an Epic 22 document's current version), publish (notifies the client's active users), view the download log.
Client: list / read published packs only; **every** item download writes ``audit.record(compliance_pack.downloaded)``
and a ``client_portal_downloads`` row. Unpublished or another client's pack -> 404 (org RLS + explicit filter).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.events.client_portal import CompliancePackPublished
from app.models.client_portal import ClientCompliancePack, ClientCompliancePackItem, ClientPortalDownload
from app.models.external_auth import ClientUser
from app.schemas.client_hub_portal import CompliancePackItemOut, CompliancePackOut
from app.schemas.client_portal_admin import (
    CompliancePackIn,
    CompliancePackInternalOut,
    CompliancePackItemInternalOut,
    DownloadLogOut,
)
from app.services.client_portal_base import client_of, ip_hash

ITEM_KINDS = ("pdf", "docx", "xlsx", "csv", "png", "jpg", "zip")


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ internal
async def _client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, write: bool) -> None:
    from app.services import clients_service

    if not await clients_service.client_access(ctx, db, client_id, write):
        raise NotFound("Client not found")


async def _pack(db: AsyncSession, client_id: uuid.UUID, pack_id: uuid.UUID) -> ClientCompliancePack:
    row = (
        await db.execute(
            select(ClientCompliancePack).where(
                ClientCompliancePack.id == pack_id, ClientCompliancePack.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Compliance pack not found")
    return row


async def _internal_out(db: AsyncSession, pack: ClientCompliancePack) -> CompliancePackInternalOut:
    items = (
        await db.execute(
            select(ClientCompliancePackItem)
            .where(ClientCompliancePackItem.pack_id == pack.id)
            .order_by(ClientCompliancePackItem.created_at)
        )
    ).scalars()
    item_list = list(items)
    downloads = (
        await db.execute(
            select(ClientPortalDownload)
            .where(ClientPortalDownload.item_id.in_([i.id for i in item_list] or [uuid.uuid4()]))
            .order_by(ClientPortalDownload.at.desc())
            .limit(500)
        )
    ).scalars()
    return CompliancePackInternalOut(
        id=pack.id,
        client_id=pack.client_id,
        period_month=pack.period_month,
        title=pack.title,
        published_at=pack.published_at,
        archived_at=pack.archived_at,
        items=[
            CompliancePackItemInternalOut(
                id=i.id, label=i.label, file_id=i.file_id, document_id=i.document_id
            )
            for i in item_list
        ],
        downloads=[
            DownloadLogOut(
                id=d.id, item_id=d.item_id, label=d.label, client_user_id=d.client_user_id, at=d.at
            )
            for d in downloads
        ],
    )


async def list_packs(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> list[CompliancePackInternalOut]:
    await _client(ctx, db, client_id, write=False)
    rows = (
        await db.execute(
            select(ClientCompliancePack)
            .where(ClientCompliancePack.client_id == client_id)
            .order_by(ClientCompliancePack.period_month.desc())
        )
    ).scalars()
    return [await _internal_out(db, r) for r in rows]


async def create_pack(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: CompliancePackIn
) -> CompliancePackInternalOut:
    await _client(ctx, db, client_id, write=True)
    row = ClientCompliancePack(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        client_id=client_id,
        period_month=data.period_month.replace(day=1),
        title=data.title,
        created_by=getattr(ctx.principal, "id", None),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await _internal_out(db, row)


async def add_file_item(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    pack_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    label: str | None,
) -> CompliancePackInternalOut:
    from app.services import files_service

    await _client(ctx, db, client_id, write=True)
    pack = await _pack(db, client_id, pack_id)
    stored = await files_service.store_upload(
        ctx, db, data, filename, ITEM_KINDS, "client_compliance_pack", pack.id
    )
    db.add(
        ClientCompliancePackItem(
            id=uuid.uuid4(),
            tenant_id=pack.tenant_id,
            pack_id=pack.id,
            file_id=stored.id,
            label=(label or stored.original_name)[:200],
        )
    )
    await db.flush()
    return await _internal_out(db, pack)


async def add_document_item(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    pack_id: uuid.UUID,
    document_id: uuid.UUID,
    label: str,
) -> CompliancePackInternalOut:
    from app.models.documents import Document

    await _client(ctx, db, client_id, write=True)
    pack = await _pack(db, client_id, pack_id)
    if (await db.execute(select(Document.id).where(Document.id == document_id))).first() is None:
        raise NotFound("Document not found")
    db.add(
        ClientCompliancePackItem(
            id=uuid.uuid4(), tenant_id=pack.tenant_id, pack_id=pack.id, document_id=document_id, label=label
        )
    )
    await db.flush()
    return await _internal_out(db, pack)


async def publish(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, pack_id: uuid.UUID
) -> CompliancePackInternalOut:
    from app.services import notifications_service as ns

    await _client(ctx, db, client_id, write=True)
    pack = await _pack(db, client_id, pack_id)
    if pack.published_at is not None:
        raise ConflictError("The pack is already published", code="pack_published")
    if (
        await db.execute(
            select(ClientCompliancePackItem.id).where(ClientCompliancePackItem.pack_id == pack.id)
        )
    ).first() is None:
        raise ConflictError("Add at least one item before publishing", code="pack_empty")
    pack.published_at = _now()
    await db.flush()
    emit(db, CompliancePackPublished(tenant_id=pack.tenant_id, pack_id=pack.id, client_id=client_id))
    users = (
        await db.execute(
            select(ClientUser.id).where(ClientUser.client_id == client_id, ClientUser.status == "active")
        )
    ).scalars()
    recipients = [ns.Recipient("client_user", u) for u in users]
    if recipients:
        await ns.deliver(
            db,
            "client_portal.compliance_pack_published",
            recipients,
            ns.Content(
                title=f"Compliance pack published: {pack.title}"[:300], link_path="/client/compliance"
            ),
            event_id=f"client_portal.compliance_pack_published:{pack.id}",
        )
    return await _internal_out(db, pack)


# ================================================================ client
async def client_packs(ctx: RequestContext, db: AsyncSession) -> list[CompliancePackOut]:
    _, client_id, _ = client_of(ctx)
    packs = list(
        (
            await db.execute(
                select(ClientCompliancePack)
                .where(
                    ClientCompliancePack.client_id == client_id,
                    ClientCompliancePack.published_at.is_not(None),
                    ClientCompliancePack.archived_at.is_(None),
                )
                .order_by(ClientCompliancePack.period_month.desc())
            )
        ).scalars()
    )
    items: dict[uuid.UUID, list[CompliancePackItemOut]] = {}
    for i in (
        await db.execute(
            select(ClientCompliancePackItem)
            .where(ClientCompliancePackItem.pack_id.in_([p.id for p in packs] or [uuid.uuid4()]))
            .order_by(ClientCompliancePackItem.created_at)
        )
    ).scalars():
        items.setdefault(i.pack_id, []).append(CompliancePackItemOut(id=i.id, label=i.label))
    return [
        CompliancePackOut(
            id=p.id,
            period_month=p.period_month,
            title=p.title,
            published_at=p.published_at,
            items=items.get(p.id, []),
        )
        for p in packs
    ]


async def client_pack(ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID) -> CompliancePackOut:
    found = [p for p in await client_packs(ctx, db) if p.id == pack_id]
    if not found:
        raise NotFound("Compliance pack not found")
    return found[0]


async def client_download(
    ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, ip: str | None
) -> tuple[bytes, str, str]:
    from app.models.documents import Document, DocumentVersion
    from app.services import files_service

    tenant_id, client_id, me = client_of(ctx)
    row = (
        await db.execute(
            select(ClientCompliancePackItem, ClientCompliancePack)
            .join(ClientCompliancePack, ClientCompliancePack.id == ClientCompliancePackItem.pack_id)
            .where(
                ClientCompliancePackItem.id == item_id,
                ClientCompliancePack.client_id == client_id,
                ClientCompliancePack.published_at.is_not(None),
                ClientCompliancePack.archived_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise NotFound("Item not found")
    item, _pack_row = row
    async with tenant_session(tenant_id) as idb:
        file_id = item.file_id
        if file_id is None and item.document_id is not None:
            file_id = (
                await idb.execute(
                    select(DocumentVersion.file_id)
                    .join(Document, Document.current_version_id == DocumentVersion.id)
                    .where(Document.id == item.document_id)
                )
            ).scalar_one_or_none()
        stored = await files_service.get_file(idb, file_id) if file_id else None
        if stored is None or stored.deleted_at is not None:
            raise NotFound("Item not found")
        data = await files_service.read_file(stored)
        filename, ctype = stored.original_name, stored.content_type
    db.add(
        ClientPortalDownload(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            client_id=client_id,
            client_user_id=me,
            item_id=item.id,
            file_id=file_id,
            label=item.label,
            at=_now(),
            ip_hash=ip_hash(ip),
        )
    )
    audit.record(
        "compliance_pack.downloaded",
        "client_compliance_pack_item",
        item.id,
        session=db,
        actor_type="client_user",
        actor_id=me,
        pack_id=str(item.pack_id),
    )
    await db.flush()
    return data, filename, ctype
