# ruff: file-ignore[line-too-long]
"""Move of the pre-vault secrets into ``vault_secrets`` (Epic 66, spec 0.10, Story 66.6, FR-336).

Run by the migration ``20261009_500_05_p5_vault_move`` (and its downgrade). FROZEN with that migration: this module
is synchronous, works on a plain SQLAlchemy ``Connection`` with SQL text, and imports only the pure modules
``app.core.kms``, ``app.services.vault.crypto``, ``app.core.security`` and settings -- never ORM models or services.

* ``provider_connector_configs.credentials`` (Fernet purpose ``connector``; the JSON text) and ``webhook_secret`` ->
  owner ``provider_config``, names ``credentials`` / ``webhook_secret``. An undecryptable value aborts the move
  (the operator must configure the key that wrote it).
* ``user_connections.access_token_enc`` / ``refresh_token_enc`` (Fernet ``TOKEN_ENCRYPTION_KEY`` or the dev key
  ``oauth_tokens``) -> owner ``calendar_connection``, names ``access_token`` / ``refresh_token``. Undecryptable tokens
  were already treated as "disconnected" by the app: they are skipped and counted, never logged.
* Per tenant (``app.tenant_id`` set by the caller): create the DEK if missing, write the rows, read every written row
  back and compare the decrypted bytes with the legacy plaintext (constant time). Any mismatch raises
  ``LegacyMoveError`` and the migration's transaction rolls back (no partial commit). Values are never logged.
"""

import dataclasses
import hmac
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from app.core.kms import Kms
from app.core.security import dev_fernet_key, key_material
from app.core.settings import get_settings
from app.services.vault import crypto

PROVIDER_CONFIG = "provider_config"
CALENDAR_CONNECTION = "calendar_connection"
OWNER_TYPES = (PROVIDER_CONFIG, CALENDAR_CONNECTION)

Key = tuple[str, uuid.UUID, str]


class LegacyMoveError(RuntimeError):
    """The legacy-secret move cannot be verified; the migration aborts."""


@dataclasses.dataclass(frozen=True, slots=True)
class LegacySecret:
    owner_type: str
    owner_id: uuid.UUID
    name: str
    plaintext: bytes = dataclasses.field(repr=False)

    @property
    def key(self) -> Key:
        return (self.owner_type, self.owner_id, self.name)


def connector_fernet() -> Fernet:
    return Fernet(key_material("connector").encode("ascii"))


def token_fernet() -> Fernet:
    s = get_settings()
    key = s.token_encryption_key
    if not key:
        if s.app_env == "production":
            msg = "TOKEN_ENCRYPTION_KEY is not configured"
            raise LegacyMoveError(msg)
        key = dev_fernet_key("oauth_tokens")
    return Fernet(key.encode("ascii"))


# ---------------------------------------------------------------- collect
def collect(
    conn: sa.Connection, tenant_id: uuid.UUID, connector: Fernet, tokens: Fernet
) -> tuple[list[LegacySecret], int]:
    """Legacy secrets of the given tenant, and the number of undecryptable tokens skipped.

    Every read filters ``tenant_id`` explicitly: the migration runs as the owner role, which bypasses RLS,
    so the ``app.tenant_id`` GUC alone does not scope these queries (see ``qa_seal_move``)."""
    found: list[LegacySecret] = []
    rows = conn.execute(
        sa.text(
            "SELECT id, credentials, webhook_secret FROM provider_connector_configs "
            "WHERE tenant_id = :tid AND (credentials IS NOT NULL OR webhook_secret IS NOT NULL) ORDER BY id"
        ),
        {"tid": tenant_id},
    ).all()
    for config_id, credentials, webhook_secret in rows:
        for name, token in (("credentials", credentials), ("webhook_secret", webhook_secret)):
            if not token:
                continue
            try:
                plain = connector.decrypt(str(token).encode("ascii"))
            except (InvalidToken, ValueError) as exc:
                msg = f"provider_connector_configs {config_id}: {name} is not decryptable with CONNECTOR_ENCRYPTION_KEY"
                raise LegacyMoveError(msg) from exc
            found.append(LegacySecret(PROVIDER_CONFIG, uuid.UUID(str(config_id)), name, plain))
    skipped = 0
    rows = conn.execute(
        sa.text(
            "SELECT id, access_token_enc, refresh_token_enc FROM user_connections "
            "WHERE tenant_id = :tid AND (access_token_enc IS NOT NULL OR refresh_token_enc IS NOT NULL) ORDER BY id"
        ),
        {"tid": tenant_id},
    ).all()
    for connection_id, access, refresh in rows:
        for name, token in (("access_token", access), ("refresh_token", refresh)):
            if not token:
                continue
            try:
                plain = tokens.decrypt(bytes(token))
            except (InvalidToken, ValueError):
                skipped += 1
                continue
            found.append(LegacySecret(CALENDAR_CONNECTION, uuid.UUID(str(connection_id)), name, plain))
    return found, skipped


# ---------------------------------------------------------------- data key
def ensure_dek(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> tuple[int, bytes]:
    row = conn.execute(
        sa.text(
            "SELECT version, wrapped_key, kek_id FROM vault_data_keys WHERE tenant_id = :tid AND status = 'active'"
        ),
        {"tid": tenant_id},
    ).first()
    if row is not None:
        return int(row[0]), kms.unwrap(bytes(row[1]), str(row[2]), str(tenant_id))
    dek = crypto.new_dek()
    wrapped, kek_id = kms.wrap(dek, str(tenant_id))
    last = conn.execute(
        sa.text("SELECT max(version) FROM vault_data_keys WHERE tenant_id = :tid"), {"tid": tenant_id}
    ).scalar()
    version = int(last or 0) + 1
    conn.execute(
        sa.text(
            "INSERT INTO vault_data_keys (id, tenant_id, version, wrapped_key, kek_id, status) "
            "VALUES (:id, :tid, :version, :wrapped, :kek_id, 'active')"
        ),
        {"id": uuid.uuid4(), "tid": tenant_id, "version": version, "wrapped": wrapped, "kek_id": kek_id},
    )
    return version, dek


def deks(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> dict[int, bytes]:
    rows = conn.execute(
        sa.text("SELECT version, wrapped_key, kek_id FROM vault_data_keys WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).all()
    return {int(v): kms.unwrap(bytes(w), str(k), str(tenant_id)) for v, w, k in rows}


# ---------------------------------------------------------------- encrypt / verify (pure)
def encrypt_rows(
    tenant_id: uuid.UUID, secrets: Iterable[LegacySecret], version: int, dek: bytes
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in secrets:
        aad = crypto.secret_aad(tenant_id, s.owner_type, s.owner_id, s.name, version)
        nonce, ct = crypto.encrypt(dek, s.plaintext, aad)
        out.append(
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "owner_type": s.owner_type,
                "owner_id": s.owner_id,
                "name": s.name,
                "ciphertext": ct,
                "nonce": nonce,
                "dek_version": version,
                "fingerprint": crypto.fingerprint(s.plaintext),
                "byte_size": len(s.plaintext),
            }
        )
    return out


def verify(
    tenant_id: uuid.UUID,
    expected: Iterable[LegacySecret],
    stored: Iterable[Mapping[str, Any]],
    dek_by_version: Mapping[int, bytes],
) -> int:
    """Every expected secret must be stored and decrypt to the byte-exact legacy plaintext; raises otherwise."""
    by_key: dict[Key, Mapping[str, Any]] = {
        (str(r["owner_type"]), uuid.UUID(str(r["owner_id"])), str(r["name"])): r for r in stored
    }
    checked = 0
    for s in expected:
        row = by_key.get(s.key)
        where = f"{s.owner_type} {s.owner_id} {s.name}"
        if row is None:
            msg = f"vault move verification failed: {where} was not written"
            raise LegacyMoveError(msg)
        dek = dek_by_version.get(int(row["dek_version"]))
        if dek is None:
            msg = f"vault move verification failed: {where} names an unknown data key"
            raise LegacyMoveError(msg)
        aad = crypto.secret_aad(tenant_id, s.owner_type, s.owner_id, s.name, int(row["dek_version"]))
        try:
            plain = crypto.decrypt(dek, bytes(row["nonce"]), bytes(row["ciphertext"]), aad)
        except crypto.InvalidTag as exc:
            msg = f"vault move verification failed: {where} does not decrypt"
            raise LegacyMoveError(msg) from exc
        if not hmac.compare_digest(plain, s.plaintext):
            msg = f"vault move verification failed: {where} differs from the legacy value"
            raise LegacyMoveError(msg)
        checked += 1
    return checked


# ---------------------------------------------------------------- per tenant
_INSERT = sa.text(
    "INSERT INTO vault_secrets (id, tenant_id, owner_type, owner_id, name, ciphertext, nonce, dek_version, "
    "fingerprint, byte_size, created_by_kind) VALUES (:id, :tid, :owner_type, :owner_id, :name, :ciphertext, :nonce, "
    ":dek_version, :fingerprint, :byte_size, 'system') "
    "ON CONFLICT (tenant_id, owner_type, owner_id, name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, "
    "nonce = EXCLUDED.nonce, dek_version = EXCLUDED.dek_version, fingerprint = EXCLUDED.fingerprint, "
    "byte_size = EXCLUDED.byte_size, updated_at = now()"
)
_STORED = sa.text(
    "SELECT owner_type, owner_id, name, ciphertext, nonce, dek_version FROM vault_secrets "
    "WHERE tenant_id = :tid AND owner_type IN ('provider_config', 'calendar_connection')"
)


def move_tenant(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> dict[str, int]:
    """Move and verify the current tenant's legacy secrets. ``app.tenant_id`` must already be set."""
    secrets, skipped = collect(conn, tenant_id, connector_fernet(), token_fernet())
    if not secrets:
        return {"moved": 0, "skipped": skipped}
    version, dek = ensure_dek(conn, tenant_id, kms)
    for params in encrypt_rows(tenant_id, secrets, version, dek):
        conn.execute(_INSERT, params)
    stored = [dict(r._mapping) for r in conn.execute(_STORED, {"tid": tenant_id}).all()]
    moved = verify(tenant_id, secrets, stored, deks(conn, tenant_id, kms))
    return {"moved": moved, "skipped": skipped}


def restore_tenant(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> int:
    """Downgrade: decrypt the moved owner types back into the legacy columns and delete their vault rows."""
    stored = [dict(r._mapping) for r in conn.execute(_STORED, {"tid": tenant_id}).all()]
    if not stored:
        return 0
    keys = deks(conn, tenant_id, kms)
    connector, tokens = connector_fernet(), token_fernet()
    columns = {
        "credentials": ("provider_connector_configs", "credentials"),
        "webhook_secret": ("provider_connector_configs", "webhook_secret"),
        "access_token": ("user_connections", "access_token_enc"),
        "refresh_token": ("user_connections", "refresh_token_enc"),
    }
    restored = 0
    for row in stored:
        target = columns.get(str(row["name"]))
        if target is None:
            continue
        aad = crypto.secret_aad(
            tenant_id, row["owner_type"], row["owner_id"], row["name"], int(row["dek_version"])
        )
        plain = crypto.decrypt(
            keys[int(row["dek_version"])], bytes(row["nonce"]), bytes(row["ciphertext"]), aad
        )
        table, column = target
        value: Any = (
            connector.encrypt(plain).decode("ascii")
            if table == "provider_connector_configs"
            else tokens.encrypt(plain)
        )
        conn.execute(
            sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": value, "id": row["owner_id"]}  # nosec B608 - table/column identifiers are in-code constants (whitelisted); values are bound params
        )
        restored += 1
    conn.execute(
        sa.text(
            "DELETE FROM vault_secrets WHERE tenant_id = :tid AND owner_type IN ('provider_config', 'calendar_connection')"
        ),
        {"tid": tenant_id},
    )
    return restored
