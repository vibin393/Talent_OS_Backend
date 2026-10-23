"""Document templates (Epic 22 A2, P2-9.1): CRUD, versions, publish (strict merge fields), preview, resolve.

Per-Legal-Entity variants are separate templates of the same ``kind``; resolution picks an exact Legal Entity
match first, else the tenant-wide template (``legal_entity_id IS NULL``). Publishing never touches documents
already generated: a generated version stores ``template_version_id`` and its rendered bytes.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.documents import DocumentTemplate, DocumentTemplateVersion
from app.schemas.documents import (
    MergeCatalogueOut,
    TemplateCreateIn,
    TemplateOut,
    TemplatePatchIn,
    TemplateVersionOut,
)
from app.services.documents import merge, render
from app.services.documents.common import conflict, not_found, now, tenant_name, tenant_of, user_id


def catalogue() -> MergeCatalogueOut:
    return MergeCatalogueOut(
        namespaces={ns: list(attrs) for ns, attrs in merge.MERGE_CATALOGUE.items()},
        filters=list(merge.FILTERS),
    )


def _version_out(v: DocumentTemplateVersion) -> TemplateVersionOut:
    return TemplateVersionOut(
        id=v.id,
        template_id=v.template_id,
        version_no=v.version_no,
        body_md=v.body_md,
        merge_fields=list(v.merge_fields or []),
        status=v.status,
        published_at=v.published_at,
        created_at=v.created_at,
    )


async def _versions(db: AsyncSession, template_id: uuid.UUID) -> list[DocumentTemplateVersion]:
    stmt = (
        select(DocumentTemplateVersion)
        .where(DocumentTemplateVersion.template_id == template_id)
        .order_by(DocumentTemplateVersion.version_no.desc())
    )
    return list((await db.execute(stmt)).scalars())


async def _out(db: AsyncSession, t: DocumentTemplate, *, with_versions: bool = False) -> TemplateOut:
    await db.refresh(t)
    versions = await _versions(db, t.id)
    current = next((v for v in versions if v.id == t.current_version_id), None)
    return TemplateOut(
        id=t.id,
        name=t.name,
        kind=t.kind,
        legal_entity_id=t.legal_entity_id,
        signer_roles=list(t.signer_roles or []),
        esign_config_id=t.esign_config_id,
        current_version_id=t.current_version_id,
        current_version_no=current.version_no if current else None,
        archived_at=t.archived_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
        versions=[_version_out(v) for v in versions] if with_versions else [],
    )


async def get_template_row(db: AsyncSession, template_id: uuid.UUID) -> DocumentTemplate:
    row = await db.get(DocumentTemplate, template_id)
    if row is None:
        raise not_found("Template")
    return row


async def list_templates(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    kind: str | None = None,
    include_archived: bool = False,
) -> list[TemplateOut]:
    stmt = select(DocumentTemplate).order_by(DocumentTemplate.kind, DocumentTemplate.name)
    if kind:
        stmt = stmt.where(DocumentTemplate.kind == kind)
    if not include_archived:
        stmt = stmt.where(DocumentTemplate.archived_at.is_(None))
    return [await _out(db, t) for t in (await db.execute(stmt)).scalars()]


async def get_template(ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID) -> TemplateOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await get_template_row(db, template_id), with_versions=True)


async def create_template(ctx: RequestContext, db: AsyncSession, data: TemplateCreateIn) -> TemplateOut:
    row = DocumentTemplate(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name.strip(),
        kind=data.kind,
        legal_entity_id=data.legal_entity_id,
        signer_roles=[r.model_dump() for r in sorted(data.signer_roles, key=lambda r: r.order)],
        esign_config_id=data.esign_config_id,
        created_by=user_id(ctx),
    )
    db.add(row)
    await db.flush()
    if data.body_md.strip():
        await add_version(ctx, db, row.id, data.body_md)
    await db.refresh(row)
    return await _out(db, row, with_versions=True)


async def patch_template(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, data: TemplatePatchIn
) -> TemplateOut:
    row = await get_template_row(db, template_id)
    fields = data.model_fields_set
    if data.name is not None:
        row.name = data.name.strip()
    if "legal_entity_id" in fields:
        row.legal_entity_id = data.legal_entity_id
    if data.signer_roles is not None:
        row.signer_roles = [r.model_dump() for r in sorted(data.signer_roles, key=lambda r: r.order)]
    if "esign_config_id" in fields:
        row.esign_config_id = data.esign_config_id
    if data.archived is not None:
        row.archived_at = now() if data.archived else None
    await db.flush()
    await db.refresh(row)
    return await get_template(ctx, db, template_id)


async def add_version(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, body_md: str
) -> TemplateVersionOut:
    """A new draft version (syntax-checked; unknown fields are reported at publish)."""
    await get_template_row(db, template_id)
    merge.referenced_fields(body_md)  # syntax / sandbox check
    no = (
        await db.execute(
            select(func.coalesce(func.max(DocumentTemplateVersion.version_no), 0)).where(
                DocumentTemplateVersion.template_id == template_id
            )
        )
    ).scalar_one()
    v = DocumentTemplateVersion(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        template_id=template_id,
        version_no=int(no) + 1,
        body_md=body_md,
        merge_fields=[],
        status="draft",
        created_by=user_id(ctx),
    )
    db.add(v)
    await db.flush()
    await db.refresh(v)
    return _version_out(v)


async def _version(db: AsyncSession, version_id: uuid.UUID) -> DocumentTemplateVersion:
    v = await db.get(DocumentTemplateVersion, version_id)
    if v is None:
        raise not_found("Template version")
    return v


async def publish_version(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> TemplateOut:
    v = await _version(db, version_id)
    if v.status != "draft":
        raise conflict("version_not_draft", "Only a draft version can be published", status=v.status)
    fields = merge.check_fields(v.body_md)  # 422 unknown_merge_field with the list
    template = await get_template_row(db, v.template_id)
    if template.current_version_id:
        prev = await db.get(DocumentTemplateVersion, template.current_version_id)
        if prev is not None and prev.status == "published":
            prev.status = "retired"
    v.merge_fields = fields
    v.status = "published"
    v.published_at = now()
    v.published_by = user_id(ctx)
    template.current_version_id = v.id
    await db.flush()
    return await get_template(ctx, db, template.id)


async def preview_version(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> bytes:  # ruff: ignore[unused-function-argument]
    """PDF of the version rendered with ``SAMPLE_CONTEXT`` (422 on unknown fields)."""
    v = await _version(db, version_id)
    template = await get_template_row(db, v.template_id)
    md = merge.render_body(v.body_md, merge.SAMPLE_CONTEXT)
    return render.render_markdown(
        md,
        header=await tenant_name(db),
        footer=f"{template.name} · preview",
        title=f"{template.name} (preview)",
    )


async def resolve_template(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    kind: str,
    legal_entity_id: uuid.UUID | None,
) -> uuid.UUID:
    """Template id for ``kind``: exact Legal Entity variant first, else the tenant-wide one (published only)."""
    stmt = select(DocumentTemplate).where(
        DocumentTemplate.kind == kind,
        DocumentTemplate.archived_at.is_(None),
        DocumentTemplate.current_version_id.is_not(None),
    )
    rows = list((await db.execute(stmt.order_by(DocumentTemplate.created_at))).scalars())
    exact = [r for r in rows if legal_entity_id is not None and r.legal_entity_id == legal_entity_id]
    generic = [r for r in rows if r.legal_entity_id is None]
    pick = (exact or generic or [None])[0]
    if pick is None:
        raise not_found("Template")
    return pick.id


async def published_version(db: AsyncSession, template_id: uuid.UUID) -> DocumentTemplateVersion:
    template = await get_template_row(db, template_id)
    if template.current_version_id is None:
        raise ValidationFailed("The template has no published version", code="template_not_published")
    v = await _version(db, template.current_version_id)
    if v.status != "published":
        raise ValidationFailed("The template has no published version", code="template_not_published")
    return v


async def resolve_variant(
    db: AsyncSession, template_id: uuid.UUID, legal_entity_id: uuid.UUID | None
) -> uuid.UUID:
    """Same kind, Legal Entity variant of ``template_id`` when one exists (else ``template_id``)."""
    t = await get_template_row(db, template_id)
    if legal_entity_id is None or t.legal_entity_id == legal_entity_id:
        return t.id
    variant = (
        await db.execute(
            select(DocumentTemplate.id).where(
                DocumentTemplate.kind == t.kind,
                DocumentTemplate.legal_entity_id == legal_entity_id,
                DocumentTemplate.archived_at.is_(None),
                DocumentTemplate.current_version_id.is_not(None),
            )
        )
    ).scalar()
    return variant or t.id


async def render_template(
    db: AsyncSession,
    template_id: uuid.UUID,
    *,
    merge_refs: dict[str, uuid.UUID] | None,
    merge_data: dict[str, Any] | None,
) -> tuple[bytes, DocumentTemplate, DocumentTemplateVersion]:
    """Merge + render the published version of ``template_id``: (pdf bytes, template, version)."""
    template = await get_template_row(db, template_id)
    v = await published_version(db, template_id)
    name = await tenant_name(db)
    context = await merge.build_context(
        db,
        v.merge_fields or merge.check_fields(v.body_md),
        merge_refs=merge_refs,
        merge_data=merge_data,
        tenant_name=name,
    )
    md = merge.render_body(v.body_md, context)
    pdf = render.render_markdown(
        md, header=name, footer=f"{template.name} · v{v.version_no}", title=template.name
    )
    return pdf, template, v
