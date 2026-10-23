# ruff: file-ignore[line-too-long]
"""Vault persistence (Epic 66, AD-82): tenant data keys, secret rows, access-log rows, platform secrets.

* ``active_dek`` lazily creates the tenant DEK (version 1, ``active``; ``INSERT .. ON CONFLICT DO NOTHING`` so racing
  creators converge). Unwrapped DEKs are cached per process for 300 s, keyed by (tenant, version, wrapped bytes).
* Secret rows are written / read with the session's tenant (RLS). Values never leave this module except as the
  return value of ``decrypt_row``; nothing here logs a value.
"""

import hashlib
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.kms import Kms, get_kms
from app.models.vault import OWNER_TYPES, PlatformSecret, VaultAccessLog, VaultDataKey, VaultSecret
from app.services.vault import crypto

DEK_CACHE_SECONDS = 300
MAX_NAME_LENGTH = 60
PLATFORM_CONTEXT = "platform"

_dek_cache: dict[tuple[str, int, str], tuple[bytes, float]] = {}


class SecretNotFound(LookupError):  # ruff: ignore[error-suffix-on-exception-name] - frozen facade name
    """No such platform secret."""


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    tenant = db.sync_session.info.get("tenant_id")
    if not tenant:
        msg = "vault secrets need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(tenant))


def check_ref(owner_type: str, name: str) -> None:
    if owner_type not in OWNER_TYPES:
        msg = f"unknown vault owner type {owner_type!r}"
        raise ValueError(msg)
    if not name or len(name) > MAX_NAME_LENGTH:
        msg = "vault secret names are 1-60 characters"
        raise ValueError(msg)


def as_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def caller_module(depth: int = 2) -> str:
    """Return the module name of the frame ``depth`` levels above the caller of this helper (the access-log ``caller``)."""
    try:
        frame = sys._getframe(depth)
    except ValueError:  # pragma: no cover - shallow stack
        return "unknown"
    return str(frame.f_globals.get("__name__", "unknown"))[:120]


# ================================================================ data keys
def clear_dek_cache(tenant_id: uuid.UUID | None = None) -> None:
    if tenant_id is None:
        _dek_cache.clear()
        return
    for key in [k for k in _dek_cache if k[0] == str(tenant_id)]:
        _dek_cache.pop(key, None)


def unwrap_cached(kms: Kms, tenant_id: uuid.UUID, version: int, wrapped: bytes, kek_id: str) -> bytes:
    key = (str(tenant_id), int(version), hashlib.sha256(bytes(wrapped)).hexdigest())
    hit = _dek_cache.get(key)
    t = time.monotonic()
    if hit is not None and hit[1] > t:
        return hit[0]
    dek = kms.unwrap(bytes(wrapped), kek_id, str(tenant_id))
    _dek_cache[key] = (dek, t + DEK_CACHE_SECONDS)
    return dek


async def _active_row(db: AsyncSession) -> VaultDataKey | None:
    return (
        await db.execute(select(VaultDataKey).where(VaultDataKey.status == "active"))
    ).scalar_one_or_none()


async def active_dek(
    db: AsyncSession, tenant_id: uuid.UUID, *, create: bool = True
) -> tuple[int, bytes] | None:
    """``(version, dek)`` of the tenant's active data key; created on first use when ``create``."""
    kms = get_kms()
    row = await _active_row(db)
    if row is None:
        if not create:
            return None
        last = (await db.execute(select(func.max(VaultDataKey.version)))).scalar_one_or_none()
        wrapped, kek_id = kms.wrap(crypto.new_dek(), str(tenant_id))
        await db.execute(
            pg_insert(VaultDataKey)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                version=int(last or 0) + 1,
                wrapped_key=wrapped,
                kek_id=kek_id,
                status="active",
            )
            .on_conflict_do_nothing()
        )
        row = await _active_row(db)
        if row is None:  # pragma: no cover - a concurrent rotation retired it in between
            msg = "could not create the tenant data key"
            raise RuntimeError(msg)
    return row.version, unwrap_cached(kms, tenant_id, row.version, row.wrapped_key, row.kek_id)


async def dek_for_version(db: AsyncSession, tenant_id: uuid.UUID, version: int) -> bytes:
    row = (await db.execute(select(VaultDataKey).where(VaultDataKey.version == version))).scalar_one_or_none()
    if row is None:
        msg = f"tenant data key v{version} is missing"
        raise LookupError(msg)
    return unwrap_cached(get_kms(), tenant_id, row.version, row.wrapped_key, row.kek_id)


async def deks_for(db: AsyncSession, tenant_id: uuid.UUID, versions: set[int]) -> dict[int, bytes]:
    if not versions:
        return {}
    kms = get_kms()
    rows = (
        await db.execute(select(VaultDataKey).where(VaultDataKey.version.in_(sorted(versions))))
    ).scalars()
    return {r.version: unwrap_cached(kms, tenant_id, r.version, r.wrapped_key, r.kek_id) for r in rows}


# ================================================================ secrets
async def load(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    name: str | None = None,
    *,
    for_update: bool = False,
) -> list[VaultSecret]:
    stmt = select(VaultSecret).where(VaultSecret.owner_type == owner_type, VaultSecret.owner_id == owner_id)
    if name is not None:
        stmt = stmt.where(VaultSecret.name == name)
    if for_update:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt.order_by(VaultSecret.name))).scalars())


def decrypt_row(tenant_id: uuid.UUID, row: VaultSecret, dek: bytes) -> bytes:
    aad = crypto.secret_aad(tenant_id, row.owner_type, row.owner_id, row.name, row.dek_version)
    return crypto.decrypt(dek, row.nonce, row.ciphertext, aad)


def is_live(row: VaultSecret) -> bool:
    return row.expires_at is None or row.expires_at > now()


async def write(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    name: str,
    value: bytes,
    *,
    meta: Mapping[str, Any] | None,
    expires_at: datetime | None,
    actor_kind: str | None,
    actor_id: uuid.UUID | None,
    rotated: bool = False,
) -> tuple[VaultSecret, bool]:
    """Upsert one secret; returns ``(row, replaced)``."""
    check_ref(owner_type, name)
    tenant_id = tenant_of(db)
    version, dek = await active_dek(db, tenant_id)  # type: ignore[misc]
    nonce, ct = crypto.encrypt(dek, value, crypto.secret_aad(tenant_id, owner_type, owner_id, name, version))
    existing = await load(db, owner_type, owner_id, name, for_update=True)
    row = existing[0] if existing else None
    if row is None:
        row = VaultSecret(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            owner_type=owner_type,
            owner_id=owner_id,
            name=name,
            created_by_kind=actor_kind,
            created_by_id=actor_id,
        )
        db.add(row)
    row.ciphertext = ct
    row.nonce = nonce
    row.dek_version = version
    row.fingerprint = crypto.fingerprint(value)
    row.byte_size = len(value)
    if meta is not None:
        row.meta = dict(meta)
    elif not existing:
        row.meta = {}
    row.expires_at = expires_at
    if existing or rotated:
        row.rotated_at = now()
    await db.flush()
    return row, bool(existing)


async def remove(
    db: AsyncSession, owner_type: str, owner_id: uuid.UUID, name: str | None = None
) -> list[str]:
    rows = await load(db, owner_type, owner_id, name)
    if not rows:
        return []
    await db.execute(delete(VaultSecret).where(VaultSecret.id.in_([r.id for r in rows])))
    return [r.name for r in rows]


def log_access(
    db: AsyncSession, tenant_id: uuid.UUID, row: VaultSecret, *, purpose: str, caller: str, actor_kind: str
) -> None:
    """One ``vault_access_log`` row per read (FR-336). Added to the session; written on its next flush."""
    db.add(
        VaultAccessLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            secret_id=row.id,
            owner_type=row.owner_type,
            owner_id=row.owner_id,
            name=row.name,
            purpose=purpose[:60],
            caller=caller[:120],
            actor_kind=actor_kind[:20],
            at=now(),
        )
    )


# ================================================================ field encryption
async def seal_field(db: AsyncSession, tenant_id: uuid.UUID, context: str, plaintext: str) -> bytes:
    version, dek = await active_dek(db, tenant_id)  # type: ignore[misc]
    return crypto.seal_blob(dek, version, context, plaintext.encode("utf-8"))


async def open_field(db: AsyncSession, tenant_id: uuid.UUID, context: str, blob: bytes) -> str:
    dek = await dek_for_version(db, tenant_id, crypto.blob_version(bytes(blob)))
    return crypto.open_blob(dek, context, blob).decode("utf-8")


# ================================================================ platform secrets
async def platform_read(pdb: AsyncSession, name: str) -> bytes:
    row = (await pdb.execute(select(PlatformSecret).where(PlatformSecret.name == name))).scalar_one_or_none()
    if row is None:
        msg = f"platform secret {name!r} is not configured"
        raise SecretNotFound(msg)
    dek = get_kms().unwrap(bytes(row.wrapped_dek), row.kek_id, PLATFORM_CONTEXT)
    return crypto.decrypt(dek, row.nonce, row.ciphertext, crypto.platform_aad(name))


async def platform_write(pdb: AsyncSession, name: str, value: bytes, *, created_by: uuid.UUID | None) -> None:
    if not name or len(name) > 80:
        msg = "platform secret names are 1-80 characters"
        raise ValueError(msg)
    dek = crypto.new_dek()
    wrapped, kek_id = get_kms().wrap(dek, PLATFORM_CONTEXT)
    nonce, ct = crypto.encrypt(dek, value, crypto.platform_aad(name))
    row = (
        await pdb.execute(select(PlatformSecret).where(PlatformSecret.name == name).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        row = PlatformSecret(id=uuid.uuid4(), name=name, created_by=created_by)
        pdb.add(row)
    else:
        row.rotated_at = now()
    row.ciphertext, row.nonce, row.wrapped_dek, row.kek_id = ct, nonce, wrapped, kek_id
    row.fingerprint = crypto.fingerprint(value)
    await pdb.flush()
    logger.info("Platform secret {} written (fingerprint {})", name, row.fingerprint)
