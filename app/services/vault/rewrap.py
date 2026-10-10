# ruff: noqa: E501
"""Vault key rotation (Epic 66, spec 0.11, FR-336).

* ``rewrap_tenant(tenant_id)`` -- a new master (KEK) version: unwrap every DEK of the tenant and wrap it again with
  the current KEK. Secret ciphertext is NOT touched (secrets are not re-encrypted).
* ``rotate_dek(tenant_id)`` -- a new DEK version (operator action, rare): retire the active DEK, create the next
  version and re-encrypt the tenant's secrets in batches. Retired DEKs stay (sealed field blobs name their version).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.database import tenant_session
from app.core.kms import get_kms
from app.models.vault import VaultDataKey, VaultSecret
from app.services.vault import crypto, store

BATCH = 200


async def _rewrap(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    kms = get_kms()
    current = kms.current_kek_id
    rows = list(
        (
            await db.execute(select(VaultDataKey).where(VaultDataKey.kek_id != current).with_for_update())
        ).scalars()
    )
    for row in rows:
        dek = kms.unwrap(bytes(row.wrapped_key), row.kek_id, str(tenant_id))
        row.wrapped_key, row.kek_id = kms.wrap(dek, str(tenant_id))
        row.rewrapped_at = store.now()
    await db.flush()
    store.clear_dek_cache(tenant_id)
    return len(rows)


async def rewrap_tenant(tenant_id: uuid.UUID, *, db: AsyncSession | None = None) -> int:
    """Rewrap the tenant's DEKs with the current KEK; returns the number of DEKs rewrapped."""
    if db is not None:
        return await _rewrap(db, tenant_id)
    async with tenant_session(tenant_id) as tdb:
        return await _rewrap(tdb, tenant_id)


async def _rotate(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    kms = get_kms()
    old = (
        await db.execute(select(VaultDataKey).where(VaultDataKey.status == "active").with_for_update())
    ).scalar_one_or_none()
    last = (await db.execute(select(func.max(VaultDataKey.version)))).scalar_one_or_none()
    if old is not None:
        old.status = "retired"
        await db.flush()
    version = int(last or 0) + 1
    dek = crypto.new_dek()
    wrapped, kek_id = kms.wrap(dek, str(tenant_id))
    db.add(
        VaultDataKey(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            version=version,
            wrapped_key=wrapped,
            kek_id=kek_id,
            status="active",
        )
    )
    await db.flush()
    moved = 0
    while True:
        rows = list(
            (
                await db.execute(
                    select(VaultSecret)
                    .where(VaultSecret.dek_version != version)
                    .limit(BATCH)
                    .with_for_update()
                )
            ).scalars()
        )
        if not rows:
            break
        old_deks = await store.deks_for(db, tenant_id, {r.dek_version for r in rows})
        for row in rows:
            value = store.decrypt_row(tenant_id, row, old_deks[row.dek_version])
            aad = crypto.secret_aad(tenant_id, row.owner_type, row.owner_id, row.name, version)
            row.nonce, row.ciphertext = crypto.encrypt(dek, value, aad)
            row.dek_version = version
        await db.flush()
        moved += len(rows)
    audit.record("vault.dek_rotated", "vault_data_key", None, session=db, version=version, secrets=moved)
    return moved


async def rotate_dek(tenant_id: uuid.UUID, *, db: AsyncSession | None = None) -> int:
    """Create a new DEK version and re-encrypt the tenant's secrets; return the number of secrets re-encrypted."""
    if db is not None:
        return await _rotate(db, tenant_id)
    async with tenant_session(tenant_id) as tdb:
        return await _rotate(tdb, tenant_id)
