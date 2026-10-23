# ruff: file-ignore[line-too-long]
"""Secrets Vault facade (Epic 66, AD-82, FR-336, NFR-63; spec 0.11 -- frozen signatures).

Envelope encryption: a per-tenant data key (DEK) in ``vault_data_keys`` wrapped by the KMS port (``core.kms``);
secrets AES-256-GCM under the DEK with AAD ``"{tenant}:{owner_type}:{owner_id}:{name}:{dek_version}"``.
No HTTP endpoint ever returns a value: APIs return ``meta`` (name, has_secret, fingerprint, rotated_at, expires_at).

* ``put`` / ``rotate`` / ``delete`` / ``delete_owner`` audit ``vault.secret_set`` / ``vault.secret_deleted`` with
  names only. ``rotate(.., keep_previous_hours=N)`` keeps the old value as ``<name>__previous`` (expires in N hours).
* ``get`` and ``SecretBag.get`` (``open_bag``) are logged reads: one ``vault_access_log`` row per read (caller module,
  purpose, actor kind) and the value is masked in later log lines of the context. Expired secrets read as ``None``.
* ``seal_field`` / ``open_field``: field-level encryption under the tenant DEK (blob = version(2) || nonce || ct,
  AAD = context); no access-log row (the owning service audits field reads).
* ``platform_get`` / ``platform_put``: platform-wide secrets (``platform_secrets``, KMS context ``"platform"``).
* ``rewrap(tenant_id)`` (new master version: DEKs rewrapped, secrets untouched) and ``rotate_dek(tenant_id)``.
"""

import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.database import platform_session
from app.services.vault import rewrap as _rewrap
from app.services.vault import store
from app.services.vault.bag import SecretBag, register_value
from app.services.vault.store import SecretNotFound

__all__ = [
    "PREVIOUS_SUFFIX",
    "SecretBag",
    "SecretMeta",
    "SecretNotFound",
    "SecretRef",
    "delete",
    "delete_owner",
    "get",
    "meta",
    "open_bag",
    "open_field",
    "platform_get",
    "platform_put",
    "put",
    "rewrap",
    "rotate",
    "rotate_dek",
    "seal_field",
]

PREVIOUS_SUFFIX = "__previous"


class SecretRef(NamedTuple):
    owner_type: str
    owner_id: uuid.UUID
    name: str


class SecretMeta(BaseModel):
    name: str
    has_secret: bool
    fingerprint: str | None = None
    rotated_at: datetime | None = None
    expires_at: datetime | None = None


def _meta(row: Any) -> SecretMeta:
    return SecretMeta(
        name=row.name,
        has_secret=True,
        fingerprint=row.fingerprint,
        rotated_at=row.rotated_at,
        expires_at=row.expires_at,
    )


def _caller() -> str:
    frame = sys._getframe(2)
    return str(frame.f_globals.get("__name__", "unknown"))


def _actor(ctx: RequestContext) -> tuple[str, uuid.UUID | None]:
    actor = actor_from_ctx(ctx)
    return actor.kind, actor.id


async def put(
    ctx: RequestContext,
    db: AsyncSession,
    ref: SecretRef,
    value: str | bytes,
    *,
    meta: Mapping[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> SecretMeta:
    kind, actor_id = _actor(ctx)
    row, _ = await store.write(
        db, ref.owner_type, ref.owner_id, ref.name, store.as_bytes(value),
        meta=meta, expires_at=expires_at, actor_kind=kind, actor_id=actor_id,
    )  # fmt: skip
    audit.record(
        "vault.secret_set", "vault_secret", row.id, session=db,
        owner_type=ref.owner_type, owner_id=str(ref.owner_id), name=ref.name,
    )  # fmt: skip
    return _meta(row)


async def delete(ctx: RequestContext, db: AsyncSession, ref: SecretRef) -> bool:  # ruff: ignore[unused-function-argument]
    store.check_ref(ref.owner_type, ref.name)
    names = await store.remove(db, ref.owner_type, ref.owner_id, ref.name)
    if names:
        audit.record(
            "vault.secret_deleted", "vault_secret", None, session=db,
            owner_type=ref.owner_type, owner_id=str(ref.owner_id), names=names,
        )  # fmt: skip
    return bool(names)


async def delete_owner(ctx: RequestContext, db: AsyncSession, owner_type: str, owner_id: uuid.UUID) -> int:  # ruff: ignore[unused-function-argument]
    store.check_ref(owner_type, "-")
    names = await store.remove(db, owner_type, owner_id)
    if names:
        audit.record(
            "vault.secret_deleted", "vault_secret", None, session=db,
            owner_type=owner_type, owner_id=str(owner_id), names=names,
        )  # fmt: skip
    return len(names)


async def meta(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
) -> list[SecretMeta]:
    return [_meta(r) for r in await store.load(db, owner_type, owner_id) if store.is_live(r)]


async def get(ctx: RequestContext, db: AsyncSession, ref: SecretRef, *, purpose: str) -> str | None:
    """Read one secret, logged (``None`` when absent or expired)."""
    caller = _caller()
    rows = await store.load(db, ref.owner_type, ref.owner_id, ref.name)
    if not rows or not store.is_live(rows[0]):
        return None
    row = rows[0]
    tenant_id = store.tenant_of(db)
    dek = await store.dek_for_version(db, tenant_id, row.dek_version)
    value = store.decrypt_row(tenant_id, row, dek).decode("utf-8")
    store.log_access(db, tenant_id, row, purpose=purpose, caller=caller, actor_kind=_actor(ctx)[0])
    register_value(value)
    return value


async def rotate(
    ctx: RequestContext,
    db: AsyncSession,
    ref: SecretRef,
    new_value: str | bytes,
    *,
    keep_previous_hours: int = 0,
) -> SecretMeta:
    """Replace a secret; with ``keep_previous_hours > 0`` the old value stays readable as ``<name>__previous``
    until it expires (signing-secret overlaps, Epic 68)."""
    kind, actor_id = _actor(ctx)
    previous = SecretRef(ref.owner_type, ref.owner_id, f"{ref.name}{PREVIOUS_SUFFIX}")
    store.check_ref(previous.owner_type, previous.name)
    current = await store.load(db, ref.owner_type, ref.owner_id, ref.name, for_update=True)
    if keep_previous_hours > 0 and current:
        tenant_id = store.tenant_of(db)
        old = store.decrypt_row(
            tenant_id, current[0], await store.dek_for_version(db, tenant_id, current[0].dek_version)
        )
        await store.write(
            db, previous.owner_type, previous.owner_id, previous.name, old,
            meta=dict(current[0].meta or {}), expires_at=store.now() + timedelta(hours=keep_previous_hours),
            actor_kind=kind, actor_id=actor_id, rotated=True,
        )  # fmt: skip
    else:
        await store.remove(db, previous.owner_type, previous.owner_id, previous.name)
    row, _ = await store.write(
        db, ref.owner_type, ref.owner_id, ref.name, store.as_bytes(new_value),
        meta=None, expires_at=None, actor_kind=kind, actor_id=actor_id, rotated=True,
    )  # fmt: skip
    audit.record(
        "vault.secret_set", "vault_secret", row.id, session=db,
        owner_type=ref.owner_type, owner_id=str(ref.owner_id), name=ref.name, rotated=True,
        keep_previous_hours=keep_previous_hours,
    )  # fmt: skip
    return _meta(row)


async def open_bag(
    ctx: RequestContext, db: AsyncSession, owner_type: str, owner_id: uuid.UUID, *, purpose: str
) -> SecretBag:
    """Lazy bag of the owner's secrets: nothing is decrypted (or logged) until ``bag.get(name)``."""
    store.check_ref(owner_type, "-")
    tenant_id = store.tenant_of(db)
    rows = {r.name: r for r in await store.load(db, owner_type, owner_id)}
    deks = await store.deks_for(db, tenant_id, {r.dek_version for r in rows.values()})
    return SecretBag(db, tenant_id, rows, deks, purpose=purpose, actor_kind=_actor(ctx)[0])


async def platform_get(name: str, *, purpose: str) -> str:
    """Read a platform-wide secret (operator-written); raise ``SecretNotFound`` when not configured."""
    caller = _caller()
    async with platform_session() as pdb:
        value = (await store.platform_read(pdb, name)).decode("utf-8")
    logger.info("Platform secret {} read (purpose={}, caller={})", name, purpose, caller)
    register_value(value)
    return value


async def platform_put(name: str, value: str | bytes, *, created_by: uuid.UUID | None = None) -> None:
    """Write a platform-wide secret (platform operators / provisioning only)."""
    async with platform_session() as pdb:
        await store.platform_write(pdb, name, store.as_bytes(value), created_by=created_by)


async def seal_field(db: AsyncSession, tenant_id: uuid.UUID, context: str, plaintext: str) -> bytes:
    return await store.seal_field(db, tenant_id, context, plaintext)


async def open_field(db: AsyncSession, tenant_id: uuid.UUID, context: str, blob: bytes) -> str:
    return await store.open_field(db, tenant_id, context, blob)


async def rewrap(tenant_id: uuid.UUID, *, db: AsyncSession | None = None) -> int:
    """Rewrap after a new master version: the tenant's DEKs only (secret ciphertext unchanged)."""
    return await _rewrap.rewrap_tenant(tenant_id, db=db)


async def rotate_dek(tenant_id: uuid.UUID, *, db: AsyncSession | None = None) -> int:
    """Rotate to a new DEK version: re-encrypt the tenant's secrets (operator action)."""
    return await _rewrap.rotate_dek(tenant_id, db=db)
