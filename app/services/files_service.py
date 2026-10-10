"""Stored files, attachments and permission-checked downloads (AD-13).

* ``store_upload`` is the only way a file enters the system: size check -> content sniffing (magic bytes;
  the declared type and extension are ignored) -> malware scan -> ``storage.put`` -> ``stored_files`` row.
* Access to a file follows its owning record: modules register ``register_access_checker(entity_type, fn)``
  and every download / attachment call re-runs that checker (``False`` -> 404).
* Attachments are generic over registered entity types; only ``candidate`` is registered in Epic 3.
"""

import contextlib
import hashlib
import io
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scanning, storage
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import forbidden
from app.core.settings import get_settings
from app.models.files import ATTACHMENT_CATEGORIES, Attachment, StoredFile
from app.schemas.files import AttachmentOut, StoredFileOut

# (ctx, db, entity_id, write) -> may the caller see (write=False) / change (write=True) the entity?
AccessChecker = Callable[[RequestContext, AsyncSession, uuid.UUID, bool], Awaitable[bool]]

DOWNLOADABLE_SCAN_STATUSES = frozenset({"clean", "skipped_dev"})

KIND_CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",  # Epic 9: career-site images
}
# Epic 58 (additive): audio recordings (voice screening). Video is never accepted.
AUDIO_CONTENT_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
}
KIND_CONTENT_TYPES.update(AUDIO_CONTENT_TYPES)
AUDIO_KINDS = frozenset(AUDIO_CONTENT_TYPES)
# Per-kind upload limits (MB); kinds not listed use ``settings.max_upload_mb``.
KIND_SIZE_LIMIT_MB: dict[str, int] = dict.fromkeys(AUDIO_KINDS, 200)
RESUME_KINDS = frozenset({"pdf", "docx"})
ATTACHMENT_KINDS = frozenset({"pdf", "docx", "png", "jpeg"})


@dataclass(frozen=True, slots=True)
class _Registration:
    entity_type: str
    plural: str
    checker: AccessChecker


_checkers: dict[str, _Registration] = {}


def register_access_checker(entity_type: str, checker: AccessChecker, *, plural: str | None = None) -> None:
    """Register who may read/write files owned by ``entity_type`` records (URL segment: ``plural``)."""
    _checkers[entity_type] = _Registration(entity_type, plural or f"{entity_type}s", checker)


def entity_type_for_path(segment: str) -> str | None:
    for reg in _checkers.values():
        if segment in {reg.plural, reg.entity_type}:
            return reg.entity_type
    return None


async def check_access(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    *,
    write: bool,
) -> bool:
    if entity_type is None or entity_id is None:
        return False
    reg = _checkers.get(entity_type)
    if reg is None:
        return False
    return await reg.checker(ctx, db, entity_id, write)


# ================================================================ upload pipeline
def sniff_kind(data: bytes) -> str | None:
    """Detect the real file kind from its bytes: pdf | docx | png | jpeg | None."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    audio = sniff_audio(data)
    if audio is not None:
        return audio
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return "docx"
        except (zipfile.BadZipFile, ValueError, OSError):
            return None
    return None


def sniff_audio(data: bytes) -> str | None:
    """Epic 58: ``wav | ogg | m4a | mp3`` from the magic bytes; containers with a video track -> None."""
    head = data[:64]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return None if (b"theora" in data[:65536] or b"\x01video" in data[:65536]) else "ogg"
    if head[4:8] == b"ftyp":
        if head[8:12] not in (b"M4A ", b"M4B ", b"mp42", b"isom", b"dash", b"mp41"):
            return None
        return None if b"vide" in data[: 4 * 1024 * 1024] else "m4a"
    if head[:3] == b"ID3" or (
        len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and head[1] != 0xFF
    ):
        return "mp3"
    return None


def _file_too_large(limit_mb: int) -> ValidationFailed:
    return ValidationFailed(
        f"The file is larger than {limit_mb} MB", code="file_too_large", details={"max_mb": limit_mb}
    )


def check_size(data: bytes, kind: str | None = None) -> None:
    limit_mb = KIND_SIZE_LIMIT_MB.get(kind or "", get_settings().max_upload_mb)
    if len(data) > limit_mb * 1024 * 1024:
        raise _file_too_large(limit_mb)


async def read_upload(upload: Any, limit_mb: int | None = None) -> bytes:
    """Read an ``UploadFile`` (or any object with ``async read(n)``) without buffering more than the limit."""
    limit_mb = limit_mb or get_settings().max_upload_mb
    data: bytes = await upload.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        raise _file_too_large(limit_mb)
    return data


def _safe_name(filename: str | None) -> str:
    name = (filename or "file").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>')
    return (name or "file")[:255]


async def store_upload(
    ctx: RequestContext,
    db: AsyncSession,
    data: bytes,
    filename: str | None,
    allowed_kinds: Iterable[str],
    owner_entity_type: str | None,
    owner_entity_id: uuid.UUID | None,
) -> StoredFile:
    """Validate, scan and persist one upload; returns the flushed ``StoredFile`` row.

    Errors (422): ``file_too_large``, ``unsupported_file_type``, ``file_infected``, ``scan_unavailable``.
    """
    tenant_id = ctx.tenant_id
    if tenant_id is None:  # pragma: no cover - tenant routes always carry a tenant
        msg = "store_upload requires a tenant context"
        raise RuntimeError(msg)
    allowed = frozenset(allowed_kinds)
    kind = sniff_kind(data)
    check_size(data, kind if kind in allowed else None)
    if kind is None or kind not in allowed:
        raise ValidationFailed(
            "Unsupported file type", code="unsupported_file_type", details={"allowed": sorted(allowed)}
        )
    result = await scanning.scan(data, _safe_name(filename))
    if result.status == "infected":
        raise ValidationFailed("The file failed the malware scan", code="file_infected")
    if result.status == "error":
        raise ValidationFailed("Malware scanning is unavailable; try again later", code="scan_unavailable")

    key = storage.new_key(tenant_id)
    content_type = KIND_CONTENT_TYPES[kind]
    await storage.get_storage().put(key, data, content_type)
    principal = ctx.principal
    row = StoredFile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        storage_key=key,
        original_name=_safe_name(filename),
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        scan_status=result.status,
        owner_entity_type=owner_entity_type,
        owner_entity_id=owner_entity_id,
        uploaded_by=getattr(principal, "id", None),
    )
    db.add(row)
    await db.flush()
    logger.info("Stored file {} ({} bytes, kind={}, scan={})", row.id, row.size_bytes, kind, row.scan_status)
    return row


async def store_generated(
    ctx: RequestContext,
    db: AsyncSession,
    data: bytes,
    *,
    key: str,
    filename: str,
    content_type: str,
    owner_entity_type: str,
    owner_entity_id: uuid.UUID,
) -> StoredFile:
    """Persist a server-generated file (Epic 11 exports: CSV / XLSX) under ``key``: malware scan (AD-13) ->
    ``storage.put`` -> ``stored_files`` row. No content sniffing (the bytes are ours); no upload size cap.

    Errors (422): ``file_infected``, ``scan_unavailable``.
    """
    tenant_id = ctx.tenant_id
    if tenant_id is None:  # pragma: no cover - tenant jobs always carry a tenant
        msg = "store_generated requires a tenant context"
        raise RuntimeError(msg)
    storage.validate_key(key)
    result = await scanning.scan(data, _safe_name(filename))
    if result.status == "infected":
        raise ValidationFailed("The file failed the malware scan", code="file_infected")
    if result.status == "error":
        raise ValidationFailed("Malware scanning is unavailable; try again later", code="scan_unavailable")
    await storage.get_storage().put(key, data, content_type)
    row = StoredFile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        storage_key=key,
        original_name=_safe_name(filename),
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        scan_status=result.status,
        owner_entity_type=owner_entity_type,
        owner_entity_id=owner_entity_id,
        uploaded_by=getattr(ctx.principal, "id", None),
    )
    db.add(row)
    await db.flush()
    return row


async def get_file(db: AsyncSession, file_id: uuid.UUID) -> StoredFile | None:
    return await db.get(StoredFile, file_id)


async def read_file(file: StoredFile) -> bytes:
    return await storage.get_storage().get(file.storage_key)


async def set_file_owner(
    db: AsyncSession, file_id: uuid.UUID, owner_entity_type: str, owner_entity_id: uuid.UUID
) -> None:
    row = await db.get(StoredFile, file_id)
    if row is not None:
        row.owner_entity_type = owner_entity_type
        row.owner_entity_id = owner_entity_id


async def purge_file(db: AsyncSession, file_id: uuid.UUID) -> bool:
    """Epic 9 (data minimisation): delete the object from storage and keep the row as a tombstone
    (``deleted_at``). Idempotent; returns whether an object was deleted."""
    row = await db.get(StoredFile, file_id)
    if row is None or row.deleted_at is not None:
        return False
    with contextlib.suppress(FileNotFoundError):
        await storage.get_storage().delete(row.storage_key)
    row.deleted_at = datetime.now(UTC)
    await db.flush()
    return True


def file_out(row: StoredFile) -> StoredFileOut:
    return StoredFileOut(
        id=row.id,
        original_name=row.original_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        scan_status=row.scan_status,
        created_at=row.created_at,
        download_url=f"/api/v1/files/{row.id}/download",
    )


# ================================================================ download
@dataclass(frozen=True, slots=True)
class Download:
    data: bytes
    filename: str
    content_type: str


async def download(ctx: RequestContext, db: AsyncSession, file_id: uuid.UUID) -> Download:
    row = await db.get(StoredFile, file_id)
    if row is None or row.scan_status not in DOWNLOADABLE_SCAN_STATUSES:
        raise NotFound("File not found")
    if not await check_access(ctx, db, row.owner_entity_type, row.owner_entity_id, write=False):
        raise NotFound("File not found")
    try:
        data = await read_file(row)
    except FileNotFoundError as exc:
        logger.error("Stored file {} is missing from storage", row.id)
        raise NotFound("File not found") from exc
    return Download(data=data, filename=row.original_name, content_type=row.content_type)


# ================================================================ attachments
def _not_found() -> NotFound:
    return NotFound("Resource not found")


# Epic 22: the attachment functions below become a facade over the documents repository. ``documents``
# registers its store from ``hooks.py`` (this module never imports a Phase 2 domain, AD-32); without a
# registered store the P1 ``attachments`` table is used.
_attachment_store: Any = None


def register_attachment_store(store: Any) -> None:
    global _attachment_store  # process-wide wiring (hooks autodiscovery)
    _attachment_store = store


async def _resolve_entity(
    ctx: RequestContext, db: AsyncSession, entity_segment: str, entity_id: uuid.UUID, *, write: bool
) -> str:
    entity_type = entity_type_for_path(entity_segment)
    if entity_type is None:
        raise _not_found()
    if not await check_access(ctx, db, entity_type, entity_id, write=False):
        raise _not_found()
    if write and not await check_access(ctx, db, entity_type, entity_id, write=True):
        raise forbidden(f"{entity_type}:update")
    return entity_type


async def _attachment_out(db: AsyncSession, row: Attachment, file: StoredFile | None = None) -> AttachmentOut:
    file = file or await db.get(StoredFile, row.file_id)
    assert file is not None
    return AttachmentOut(
        id=row.id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        category=row.category,
        expires_on=row.expires_on,
        note=row.note,
        archived_at=row.archived_at,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        file=file_out(file),
    )


async def list_attachments(
    ctx: RequestContext,
    db: AsyncSession,
    entity_segment: str,
    entity_id: uuid.UUID,
    *,
    include_archived: bool = False,
) -> list[AttachmentOut]:
    entity_type = await _resolve_entity(ctx, db, entity_segment, entity_id, write=False)
    if _attachment_store is not None:
        return list(
            await _attachment_store.list(ctx, db, entity_type, entity_id, include_archived=include_archived)
        )
    stmt = (
        select(Attachment, StoredFile)
        .join(StoredFile, StoredFile.id == Attachment.file_id)
        .where(Attachment.entity_type == entity_type, Attachment.entity_id == entity_id)
        .order_by(Attachment.created_at.desc(), Attachment.id)
    )
    if not include_archived:
        stmt = stmt.where(Attachment.archived_at.is_(None))
    rows = (await db.execute(stmt)).all()
    return [await _attachment_out(db, att, file) for att, file in rows]


async def add_attachment(
    ctx: RequestContext,
    db: AsyncSession,
    entity_segment: str,
    entity_id: uuid.UUID,
    *,
    data: bytes,
    filename: str | None,
    category: str,
    expires_on: date | None,
    note: str | None,
) -> AttachmentOut:
    entity_type = await _resolve_entity(ctx, db, entity_segment, entity_id, write=True)
    if category not in ATTACHMENT_CATEGORIES:
        raise ValidationFailed(
            "Unknown attachment category",
            details={
                "fields": [{"field": "category", "message": f"must be one of {list(ATTACHMENT_CATEGORIES)}"}]
            },
        )
    if _attachment_store is not None:
        return await _attachment_store.add(  # type: ignore[no-any-return]
            ctx, db, entity_type, entity_id, data=data, filename=filename, category=category,
            expires_on=expires_on, note=note,
        )  # fmt: skip
    file = await store_upload(ctx, db, data, filename, ATTACHMENT_KINDS, entity_type, entity_id)
    row = Attachment(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        file_id=file.id,
        category=category,
        expires_on=expires_on,
        note=(note or None),
        uploaded_by=getattr(ctx.principal, "id", None),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await db.refresh(file)
    return await _attachment_out(db, row, file)


async def archive_attachment(
    ctx: RequestContext, db: AsyncSession, attachment_id: uuid.UUID
) -> AttachmentOut:
    if _attachment_store is not None:
        owner = await _attachment_store.owner(db, attachment_id)
        reg = _checkers.get(owner[0]) if owner else None
        if owner is None or reg is None:
            raise _not_found()
        await _resolve_entity(ctx, db, reg.entity_type, owner[1], write=True)
        return await _attachment_store.archive(ctx, db, attachment_id)  # type: ignore[no-any-return]
    row = await db.get(Attachment, attachment_id)
    if row is None:
        raise _not_found()
    reg = _checkers.get(row.entity_type)
    if reg is None:
        raise _not_found()
    await _resolve_entity(ctx, db, reg.entity_type, row.entity_id, write=True)
    if row.archived_at is None:
        row.archived_at = datetime.now(UTC)
        await db.flush()
    await db.refresh(row)
    return await _attachment_out(db, row)


async def reassign_attachments(
    db: AsyncSession,
    entity_type: str,
    from_entity_id: uuid.UUID,
    to_entity_id: uuid.UUID,
    *,
    only_ids: Iterable[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Move attachments (and their files' ownership) between two records, e.g. on candidate merge/undo.

    Returns the moved attachment ids.
    """
    if _attachment_store is not None:
        return list(
            await _attachment_store.reassign(db, entity_type, from_entity_id, to_entity_id, only_ids=only_ids)
        )
    stmt = select(Attachment).where(Attachment.entity_type == entity_type)
    if only_ids is not None:
        ids = list(only_ids)
        if not ids:
            return []
        stmt = stmt.where(Attachment.id.in_(ids))
    else:
        stmt = stmt.where(Attachment.entity_id == from_entity_id)
    moved: list[uuid.UUID] = []
    for att in (await db.execute(stmt)).scalars():
        att.entity_id = to_entity_id
        moved.append(att.id)
        file = await db.get(StoredFile, att.file_id)
        if file is not None and file.owner_entity_type == entity_type:
            file.owner_entity_id = to_entity_id
    await db.flush()
    return moved


async def count_attachments(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> int:
    if _attachment_store is not None:
        return int(await _attachment_store.count(db, entity_type, entity_id))
    stmt = (
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.entity_type == entity_type,
            Attachment.entity_id == entity_id,
            Attachment.archived_at.is_(None),
        )
    )
    return int((await db.execute(stmt)).scalar_one())
