"""Fernet encryption of OAuth tokens at rest (``TOKEN_ENCRYPTION_KEY``).

Without a key (only possible while no real provider is configured -- settings refuse it otherwise) a
deterministic development key is used, never in production.
"""

import functools

from cryptography.fernet import Fernet, InvalidToken

from app.core.security import dev_fernet_key
from app.core.settings import get_settings


@functools.cache
def _fernet(key: str) -> Fernet:
    return Fernet(key.encode("ascii"))


def fernet() -> Fernet:
    s = get_settings()
    key = s.token_encryption_key
    if not key:
        if s.app_env == "production":
            msg = "TOKEN_ENCRYPTION_KEY is not configured"
            raise RuntimeError(msg)
        key = dev_fernet_key("oauth_tokens")
    return _fernet(key)


def encrypt(value: str | None) -> bytes | None:
    if value is None:
        return None
    return fernet().encrypt(value.encode("utf-8"))


def decrypt(value: bytes | None) -> str | None:
    """Plain token, or None when absent / not decryptable with the current key (treated as disconnected)."""
    if not value:
        return None
    try:
        return fernet().decrypt(bytes(value)).decode("utf-8")
    except InvalidToken:
        return None
