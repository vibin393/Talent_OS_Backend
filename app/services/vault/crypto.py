# ruff: file-ignore[line-too-long]
"""Vault envelope primitives (Epic 66, AD-82). PURE: no database, no settings, no service imports.

Imported by the migration ``20261009_500_05_p5_vault_move``: keep it dependency-free (``cryptography`` only).

* DEK: 32 random bytes per tenant (``new_dek``), stored wrapped by the KMS port (``core.kms``).
* Secrets: AES-256-GCM, 12-byte nonce, AAD ``"{tenant}:{owner_type}:{owner_id}:{name}:{dek_version}"``.
* Field blobs (``seal_field``): ``version (2 bytes, big endian) || nonce (12) || ciphertext``, AAD = the caller's context.
* Platform secrets: AAD ``"platform:{name}"`` under a per-secret DEK wrapped with KMS context ``"platform"``.
"""

import hashlib
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "DEK_BYTES",
    "NONCE_BYTES",
    "InvalidTag",
    "decrypt",
    "encrypt",
    "fingerprint",
    "new_dek",
    "open_blob",
    "platform_aad",
    "seal_blob",
    "secret_aad",
]

DEK_BYTES = 32
NONCE_BYTES = 12
_VERSION_BYTES = 2


def new_dek() -> bytes:
    return os.urandom(DEK_BYTES)


def secret_aad(
    tenant_id: uuid.UUID | str, owner_type: str, owner_id: uuid.UUID | str, name: str, dek_version: int
) -> bytes:
    return f"{uuid.UUID(str(tenant_id))}:{owner_type}:{uuid.UUID(str(owner_id))}:{name}:{int(dek_version)}".encode()


def platform_aad(name: str) -> bytes:
    return f"platform:{name}".encode()


def encrypt(dek: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """``(nonce, ciphertext)``; the GCM tag is appended to the ciphertext."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(bytes(dek)).encrypt(nonce, bytes(plaintext), aad)


def decrypt(dek: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """Decrypt; raise ``InvalidTag`` on a wrong key, a wrong AAD or tampered ciphertext."""
    return AESGCM(bytes(dek)).decrypt(bytes(nonce), bytes(ciphertext), aad)


def fingerprint(value: bytes) -> str:
    """Four hex characters of SHA-256(value): identifies a secret without revealing any of it."""
    return hashlib.sha256(bytes(value)).hexdigest()[-4:]


def seal_blob(dek: bytes, dek_version: int, context: str, plaintext: bytes) -> bytes:
    nonce, ct = encrypt(dek, plaintext, context.encode("utf-8"))
    return int(dek_version).to_bytes(_VERSION_BYTES, "big") + nonce + ct


def blob_version(blob: bytes) -> int:
    if len(blob) < _VERSION_BYTES + NONCE_BYTES + 16:
        msg = "sealed field blob is too short"
        raise ValueError(msg)
    return int.from_bytes(bytes(blob[:_VERSION_BYTES]), "big")


def open_blob(dek: bytes, context: str, blob: bytes) -> bytes:
    raw = bytes(blob)
    blob_version(raw)
    nonce = raw[_VERSION_BYTES : _VERSION_BYTES + NONCE_BYTES]
    return decrypt(dek, nonce, raw[_VERSION_BYTES + NONCE_BYTES :], context.encode("utf-8"))
