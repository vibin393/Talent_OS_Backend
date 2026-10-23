# ruff: file-ignore[line-too-long]
"""Key-management port for the Secrets Vault (Epic 66, AD-82, D-14). Pure: never imports ``app.services``.

* ``Kms.wrap(plaintext_key, context) -> (wrapped, kek_id)`` / ``Kms.unwrap(wrapped, kek_id, context) -> bytes``.
  ``context`` binds the wrap to its owner (a tenant id, or ``"platform"``).
* ``LocalKms`` (default, ``settings.vault_kms_provider == "local"``): KEK = HKDF-SHA256(master key
  ``vault_master_keys[str(version)]`` (base64, 32 bytes), ``info="vault-kek:<context>"``), AES key wrap (RFC 3394).
  ``kek_id = "local:v<version>"``; wraps always use ``vault_current_master_version``, unwraps the version in the id.
  Without configured master keys a deterministic development master (from ``dev_fernet_key("vault")``) is used
  outside production; production refuses it.
* A managed KMS adapter (AWS KMS / Azure Key Vault) is a later drop-in behind the same port (D-14): no data migration,
  ``vault_data_keys.kek_id`` records which KEK wrapped each DEK.

Used by the migration ``20261009_500_05_p5_vault_move``: keep this module free of database and service imports.
"""

import base64
import binascii
import functools
import hashlib
from collections.abc import Mapping
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from app.core.security import dev_fernet_key
from app.core.settings import get_settings

__all__ = ["InvalidUnwrap", "Kms", "KmsError", "LocalKms", "get_kms", "set_kms"]

LOCAL_PREFIX = "local:v"


class KmsError(RuntimeError):
    """The KMS cannot wrap / unwrap (unknown KEK, missing master key, refused dev key)."""


class Kms(Protocol):
    def wrap(self, plaintext_key: bytes, context: str) -> tuple[bytes, str]: ...

    def unwrap(self, wrapped: bytes, kek_id: str, context: str) -> bytes: ...

    @property
    def current_kek_id(self) -> str: ...


def _dev_master(version: int) -> bytes:
    seed = base64.urlsafe_b64decode(dev_fernet_key("vault").encode("ascii"))
    return hashlib.sha256(seed + f":vault-master:v{version}".encode()).digest()


@functools.cache
def _kek(master: bytes, context: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"vault-kek:{context}".encode()).derive(
        master
    )


class LocalKms:
    """Local key-derivation KMS. ``master_keys`` maps a version to the raw 32-byte master (tests inject them);
    ``None`` reads ``settings.vault_master_keys`` (or the dev master outside production)."""

    def __init__(
        self, master_keys: Mapping[int, bytes] | None = None, current_version: int | None = None
    ) -> None:
        s = get_settings()
        self.current_version = int(current_version or s.vault_current_master_version)
        if master_keys is not None:
            self._keys = {int(version): bytes(key) for version, key in master_keys.items()}
            self._dev = False
        else:
            self._keys = {}
            for version, encoded in (s.vault_master_keys or {}).items():
                try:
                    raw = base64.b64decode(str(encoded).encode("ascii"), validate=False)
                except (binascii.Error, ValueError) as exc:
                    msg = f"vault master key v{version} is not valid base64"
                    raise KmsError(msg) from exc
                if len(raw) != 32:
                    msg = f"vault master key v{version} must decode to 32 bytes"
                    raise KmsError(msg)
                self._keys[int(version)] = raw
            self._dev = not self._keys
            if self._dev and s.app_env == "production":
                msg = "VAULT_MASTER_KEYS is not configured (the development master is refused in production)"
                raise KmsError(msg)

    @property
    def current_kek_id(self) -> str:
        return f"{LOCAL_PREFIX}{self.current_version}"

    def _master(self, version: int) -> bytes:
        if self._dev:
            return _dev_master(version)
        key = self._keys.get(version)
        if key is None:
            msg = f"vault master key v{version} is not configured"
            raise KmsError(msg)
        return key

    @staticmethod
    def _version(kek_id: str) -> int:
        if not kek_id.startswith(LOCAL_PREFIX) or not kek_id[len(LOCAL_PREFIX) :].isdigit():
            msg = f"unknown KEK id {kek_id!r} for the local KMS"
            raise KmsError(msg)
        return int(kek_id[len(LOCAL_PREFIX) :])

    def wrap(self, plaintext_key: bytes, context: str) -> tuple[bytes, str]:
        kek = _kek(self._master(self.current_version), context)
        return aes_key_wrap(kek, bytes(plaintext_key)), self.current_kek_id

    def unwrap(self, wrapped: bytes, kek_id: str, context: str) -> bytes:
        kek = _kek(self._master(self._version(kek_id)), context)
        return aes_key_unwrap(kek, bytes(wrapped))


_override: Kms | None = None


def set_kms(kms: Kms | None) -> None:
    """Install a KMS for the process (tests, rotation drills); ``None`` restores the configured one."""
    global _override  # process-wide wiring
    _override = kms


def get_kms() -> Kms:
    if _override is not None:
        return _override
    provider = get_settings().vault_kms_provider
    if provider == "local":
        return LocalKms()
    msg = f"unknown vault KMS provider {provider!r}"  # type: ignore[unreachable]  # pragma: no cover - settings Literal refuses others
    raise KmsError(msg)
