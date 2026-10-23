# ruff: file-ignore[line-too-long]
"""Knowledge Sources admin (Epic 62 A1, story 62.4; ``knowledge:manage`` at the router, audited rows).

Versions are append-only: every content change is a new version (markdown, or an uploaded PDF / DOCX / TXT up to
10 MB, scanned by ``files_service``; text extracted with the P1 resume extractor, ``unsupported`` otherwise). Publishing
makes exactly that version's chunks searchable; unpublishing / archiving switches them off **synchronously**.
``import_onboarding`` creates one draft source per active onboarding template (task labels + instructions) and per
``policy`` document template (latest published version), each only once.
"""

import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.events.knowledge import KnowledgeSourcePublished, KnowledgeSourceUnpublished
from app.models.knowledge import KnowledgeChunk, KnowledgeSource, KnowledgeSourceVersion
from app.schemas.knowledge import (
    ImportResultOut,
    SourceCreate,
    SourceDetailOut,
    SourceOut,
    SourceUpdate,
    VersionOut,
)
from app.services.knowledge import index
from app.services.knowledge.common import conflict, not_found, tenant_of, user_id

FILE_LIMIT_MB = 10
FILE_KINDS = ("pdf", "docx")


def source_out(s: KnowledgeSource) -> SourceOut:
    return SourceOut(
        id=s.id, title=s.title, kind=s.kind, audience=s.audience, status=s.status,  # type: ignore[arg-type]
        current_version_no=s.current_version_no, published_version_no=s.published_version_no, ref_kind=s.ref_kind,
        ref_id=s.ref_id, legal_entity_id=s.legal_entity_id, client_id=s.client_id, owner_id=s.owner_id,
        tags=list(s.tags or []), last_reviewed_on=s.last_reviewed_on, created_at=s.created_at, updated_at=s.updated_at,
    )  # fmt: skip


async def _get(db: AsyncSession, source_id: uuid.UUID, *, lock: bool = False) -> KnowledgeSource:
    stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found("Knowledge source")
    return row


async def list_sources(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    status: str | None = None,
    audience: str | None = None,
    q: str | None = None,
) -> list[SourceOut]:
    stmt = select(KnowledgeSource).order_by(KnowledgeSource.updated_at.desc())
    if status:
        stmt = stmt.where(KnowledgeSource.status == status)
    else:
        stmt = stmt.where(KnowledgeSource.status != "archived")
    if audience:
        stmt = stmt.where(KnowledgeSource.audience == audience)
    if q:
        stmt = stmt.where(KnowledgeSource.title.ilike(f"%{q.strip()[:100]}%"))
    return [source_out(s) for s in (await db.execute(stmt.limit(500))).scalars()]


async def _versions(db: AsyncSession, s: KnowledgeSource) -> list[VersionOut]:
    rows = list(
        (
            await db.execute(
                select(KnowledgeSourceVersion)
                .where(KnowledgeSourceVersion.source_id == s.id)
                .order_by(KnowledgeSourceVersion.version_no.desc())
            )
        ).scalars()
    )
    counts = dict(
        (
            await db.execute(
                select(KnowledgeChunk.version_no, func.count())
                .where(KnowledgeChunk.source_id == s.id)
                .group_by(KnowledgeChunk.version_no)
            )
        ).all()
    )
    return [
        VersionOut(
            id=v.id,
            version_no=v.version_no,
            body_md=v.body_md,
            file_id=v.file_id,
            extract_status=v.extract_status,
            extracted_chars=len(v.extracted_text or ""),
            chunk_count=int(counts.get(v.version_no, 0)),
            published=s.status == "published" and s.published_version_no == v.version_no,
            created_by=v.created_by,
            created_at=v.created_at,
        )
        for v in rows
    ]


async def get_source(ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID) -> SourceDetailOut:  # ruff: ignore[unused-function-argument]
    s = await _get(db, source_id)
    versions = await _versions(db, s)
    current = next((v for v in versions if v.version_no == s.current_version_no), None)
    return SourceDetailOut(
        **source_out(s).model_dump(), current_body_md=current.body_md if current else None, versions=versions
    )


async def list_versions(ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID) -> list[VersionOut]:  # ruff: ignore[unused-function-argument]
    return await _versions(db, await _get(db, source_id))


def _sha(text: str | None, data: bytes | None = None) -> str:
    return hashlib.sha256(data if data is not None else (text or "").encode()).hexdigest()


async def _add_version(
    ctx: RequestContext,
    db: AsyncSession,
    s: KnowledgeSource,
    *,
    body_md: str | None = None,
    file_id: uuid.UUID | None = None,
    extracted: str | None = None,
    extract_status: str = "ok",
    digest: str | None = None,
) -> KnowledgeSourceVersion:
    s.current_version_no = (s.current_version_no or 0) + 1
    v = KnowledgeSourceVersion(
        id=uuid.uuid4(), tenant_id=s.tenant_id, source_id=s.id, version_no=s.current_version_no, body_md=body_md,
        file_id=file_id, extracted_text=extracted, extract_status=extract_status,
        content_sha256=digest or _sha(body_md if body_md is not None else extracted), created_by=user_id(ctx),
    )  # fmt: skip
    db.add(v)
    await db.flush()
    await index.build_chunks(db, s, v)
    return v


async def create_source(ctx: RequestContext, db: AsyncSession, data: SourceCreate) -> SourceDetailOut:
    s = KnowledgeSource(
        id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), title=data.title.strip(), kind=data.kind, audience=data.audience,
        status="draft", current_version_no=0, legal_entity_id=data.legal_entity_id, client_id=data.client_id,
        owner_id=user_id(ctx), tags=[t.strip()[:40] for t in data.tags if t.strip()], last_reviewed_on=data.last_reviewed_on,
    )  # fmt: skip
    db.add(s)
    await db.flush()
    if data.body_md and data.body_md.strip():
        await _add_version(ctx, db, s, body_md=data.body_md)
    return await get_source(ctx, db, s.id)


async def update_source(
    ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID, data: SourceUpdate
) -> SourceDetailOut:
    s = await _get(db, source_id, lock=True)
    if s.status == "archived":
        raise conflict("source_archived", "An archived source cannot be changed")
    fields = data.model_dump(exclude_unset=True)
    if "title" in fields and data.title:
        s.title = data.title.strip()
    if "audience" in fields and data.audience:
        s.audience = data.audience
    if "tags" in fields and data.tags is not None:
        s.tags = [t.strip()[:40] for t in data.tags if t.strip()]
    for key in ("legal_entity_id", "client_id", "last_reviewed_on"):
        if key in fields:
            setattr(s, key, fields[key])
    await db.flush()
    await index.sync_source(db, s)
    return await get_source(ctx, db, s.id)


async def new_version(
    ctx: RequestContext,
    db: AsyncSession,
    source_id: uuid.UUID,
    *,
    body_md: str | None = None,
    file: tuple[bytes, str | None] | None = None,
) -> VersionOut:
    s = await _get(db, source_id, lock=True)
    if s.status == "archived":
        raise conflict("source_archived", "An archived source cannot be changed")
    if file is None:
        if not body_md or not body_md.strip():
            raise ValidationFailed("The article needs some text", code="body_required")
        v = await _add_version(ctx, db, s, body_md=body_md)
    else:
        v = await _file_version(ctx, db, s, *file)
    if s.kind == "article" and file is not None:
        s.kind = "file"
    await db.flush()
    versions = await _versions(db, s)
    return next(x for x in versions if x.version_no == v.version_no)


async def _file_version(
    ctx: RequestContext, db: AsyncSession, s: KnowledgeSource, data: bytes, filename: str | None
) -> KnowledgeSourceVersion:
    from app.services import files_service

    if len(data) > FILE_LIMIT_MB * 1024 * 1024:
        raise ValidationFailed(
            f"The file is larger than {FILE_LIMIT_MB} MB",
            code="file_too_large",
            details={"max_mb": FILE_LIMIT_MB},
        )
    name = (filename or "").lower()
    if name.endswith((".txt", ".md")) or files_service.sniff_kind(data) is None:
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded is None or not name.endswith((".txt", ".md")):
            return await _add_version(
                ctx, db, s, extracted=None, extract_status="unsupported", digest=_sha(None, data)
            )
        return await _add_version(
            ctx, db, s, extracted=decoded[:100_000], extract_status="ok", digest=_sha(None, data)
        )
    stored = await files_service.store_upload(ctx, db, data, filename, FILE_KINDS, "knowledge_source", s.id)
    kind = files_service.sniff_kind(data) or ""
    from app.services.candidates.extraction import extract_text

    result = extract_text(data, kind)
    status = "ok" if result.status == "ok" else "failed"
    return await _add_version(
        ctx, db, s, file_id=stored.id, extracted=(result.text or "")[:100_000] or None, extract_status=status,
        digest=_sha(None, data),
    )  # fmt: skip


async def publish(
    ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID, version_no: int
) -> SourceDetailOut:
    s = await _get(db, source_id, lock=True)
    if s.status == "archived":
        raise conflict("source_archived", "An archived source cannot be published")
    v = (
        await db.execute(
            select(KnowledgeSourceVersion).where(
                KnowledgeSourceVersion.source_id == s.id, KnowledgeSourceVersion.version_no == version_no
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise not_found("Version")
    if v.extract_status not in ("ok",) or not (v.body_md or v.extracted_text):
        raise conflict("version_not_publishable", "This version has no readable text to publish")
    s.status = "published"
    s.published_version_no = version_no
    await db.flush()
    await index.sync_source(db, s)
    emit(
        db,
        KnowledgeSourcePublished(
            tenant_id=s.tenant_id, source_id=s.id, version_no=version_no, audience=s.audience
        ),
    )
    return await get_source(ctx, db, s.id)


async def unpublish(ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID) -> SourceDetailOut:
    s = await _get(db, source_id, lock=True)
    if s.status != "published":
        raise conflict("source_not_published", "The source is not published")
    s.status = "unpublished"
    await db.flush()
    await index.sync_source(db, s)
    emit(db, KnowledgeSourceUnpublished(tenant_id=s.tenant_id, source_id=s.id))
    return await get_source(ctx, db, s.id)


async def archive(ctx: RequestContext, db: AsyncSession, source_id: uuid.UUID) -> SourceDetailOut:
    s = await _get(db, source_id, lock=True)
    was_published = s.status == "published"
    s.status = "archived"
    await db.flush()
    await index.sync_source(db, s)
    if was_published:
        emit(db, KnowledgeSourceUnpublished(tenant_id=s.tenant_id, source_id=s.id))
    return await get_source(ctx, db, s.id)


def _template_markdown(name: str, tasks: list[Any]) -> str:
    lines = [f"# {name}", ""]
    for t in tasks:
        lines.append(f"## {t.label}")
        lines.append("")
        lines.append((t.instructions or "Complete this step in your onboarding tasks.").strip())
        lines.append("")
    return "\n".join(lines).strip()


async def import_onboarding(ctx: RequestContext, db: AsyncSession) -> ImportResultOut:
    """One **draft** source per active onboarding template and per ``policy`` document template (each once)."""
    from app.models.documents import DocumentTemplate, DocumentTemplateVersion
    from app.models.onboarding import OnboardingTemplate, OnboardingTemplateTask

    existing = {
        (k, r)
        for k, r in (
            await db.execute(
                select(KnowledgeSource.ref_kind, KnowledgeSource.ref_id).where(
                    KnowledgeSource.ref_id.is_not(None)
                )
            )
        ).all()
    }
    created: list[uuid.UUID] = []
    skipped = 0
    templates = list(
        (
            await db.execute(
                select(OnboardingTemplate).where(
                    OnboardingTemplate.active.is_(True), OnboardingTemplate.archived_at.is_(None)
                )
            )
        ).scalars()
    )
    for t in templates:
        if ("onboarding_template", t.id) in existing:
            skipped += 1
            continue
        tasks = list(
            (
                await db.execute(
                    select(OnboardingTemplateTask)
                    .where(
                        OnboardingTemplateTask.template_id == t.id,
                        OnboardingTemplateTask.assignee == "worker",
                    )
                    .order_by(OnboardingTemplateTask.position)
                )
            ).scalars()
        )
        s = KnowledgeSource(
            id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), title=f"Onboarding: {t.name}"[:200], kind="onboarding_template",
            audience="both", status="draft", current_version_no=0, ref_kind="onboarding_template", ref_id=t.id,
            legal_entity_id=t.legal_entity_id, client_id=t.client_id, owner_id=user_id(ctx), tags=["onboarding"],
        )  # fmt: skip
        db.add(s)
        await db.flush()
        await _add_version(ctx, db, s, body_md=_template_markdown(t.name, tasks)[:100_000])
        created.append(s.id)
    policies = list(
        (
            await db.execute(
                select(DocumentTemplate).where(
                    DocumentTemplate.kind == "policy", DocumentTemplate.archived_at.is_(None)
                )
            )
        ).scalars()
    )
    for d in policies:
        if ("document_template", d.id) in existing:
            skipped += 1
            continue
        version = (
            await db.execute(
                select(DocumentTemplateVersion)
                .where(
                    DocumentTemplateVersion.template_id == d.id, DocumentTemplateVersion.status == "published"
                )
                .order_by(DocumentTemplateVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if version is None:
            skipped += 1
            continue
        s = KnowledgeSource(
            id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), title=d.name[:200], kind="policy_document", audience="both",
            status="draft", current_version_no=0, ref_kind="document_template", ref_id=d.id, owner_id=user_id(ctx),
            tags=["policy"],
        )  # fmt: skip
        db.add(s)
        await db.flush()
        await _add_version(ctx, db, s, body_md=version.body_md[:100_000])
        created.append(s.id)
    return ImportResultOut(created=len(created), skipped=skipped, source_ids=created)


__all__ = [
    "archive",
    "create_source",
    "get_source",
    "import_onboarding",
    "list_sources",
    "list_versions",
    "new_version",
    "publish",
    "source_out",
    "unpublish",
    "update_source",
]
